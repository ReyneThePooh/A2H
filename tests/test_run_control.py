"""Offline failure-injection checks for repair run safety."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace as NS

import pytest

import run_control as rc
from pipeline.agents import IncompleteResponseError, LLMCallError, PipelineAgent, RetryingLLM


def test_budget_counts_retries_and_nested_scope_restores():
    outer = rc.Budget(max_llm_calls=2, max_builds=1)
    with rc.budget_scope(outer):
        rc.consume_budget("llm_calls")
        with rc.budget_scope(rc.Budget(max_llm_calls=0)):
            with pytest.raises(rc.BudgetExceeded, match="max_llm_calls"):
                rc.consume_budget("llm_calls")
        rc.consume_budget("llm_calls")
        with pytest.raises(rc.BudgetExceeded, match="max_llm_calls"):
            rc.consume_budget("llm_calls")
        rc.consume_budget("steps", 7)
    assert outer.counts["llm_calls"] == 2
    assert outer.snapshot()["counts"]["steps"] == 7
    assert rc.current_budget() is None


def test_deadline_scope_limits_time_but_retains_global_counts(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(rc.time, "monotonic", lambda: clock[0])
    budget = rc.Budget(time_limit_s=10)
    with rc.budget_scope(budget):
        assert rc.remaining_timeout(30) == 10
        with rc.deadline_scope(3):
            rc.consume_budget("replays")
            assert rc.remaining_timeout(30) == 3
            clock[0] += 4
            with pytest.raises(rc.BudgetExceeded, match="stage_time_limit"):
                rc.check_budget()
        assert rc.remaining_timeout(30) == 6
    restored = rc.Budget(time_limit_s=10)
    restored.restore(budget.snapshot())
    assert restored.counts["replays"] == 1
    assert restored.remaining_timeout(30) == 6


def test_budget_persists_each_consumption_before_network_work(tmp_path):
    journal = rc.RunJournal(tmp_path)
    budget = rc.Budget(max_llm_calls=3)
    budget.snapshot_callback = lambda snapshot: journal.update(budget=snapshot)
    budget.consume("llm_calls")
    saved = journal.state["budget"]
    assert saved["counts"]["llm_calls"] == 1
    budget.restore(saved)
    assert journal.state["budget"]["counts"]["llm_calls"] == 1


def test_journal_marks_interrupted_stage_and_rejects_changed_resume(tmp_path):
    journal = rc.RunJournal(tmp_path)
    journal.bind_inputs({"project_sha256": "base", "api_key": "private-key"})
    journal.begin_stage("build")
    resumed = rc.RunJournal(tmp_path, journal.run_id)
    resumed.bind_inputs({"project_sha256": "base", "api_key": "private-key"}, resume=True)
    assert resumed.state["stages"]["build"]["status"] == "interrupted"
    with pytest.raises(rc.ResumeMismatch):
        resumed.bind_inputs({"project_sha256": "changed"}, resume=True)
    assert "private-key" not in resumed.state_path.read_text(encoding="utf-8")
    events = sorted((tmp_path / "events").glob("*.json"))
    assert [json.loads(p.read_text())["sequence"] for p in events] == [1, 2, 3]


def test_journal_records_failure_without_exception_secrets(tmp_path):
    journal = rc.RunJournal(tmp_path)
    with pytest.raises(RuntimeError):
        with journal.stage("model"):
            raise RuntimeError("password=private")
    assert journal.state["stages"]["model"]["status"] == "failed"
    assert "password=private" not in journal.state_path.read_text()


def test_exclusive_lock_survives_stale_file(tmp_path):
    path = tmp_path / "lock"
    path.write_text("stale old PID")
    with rc.FileLock(path):
        with pytest.raises(rc.LockUnavailable):
            with rc.FileLock(path):
                pass
    with rc.FileLock(path):
        pass


def test_killed_process_releases_os_lock(tmp_path):
    lock_path, ready = tmp_path / "child.lock", tmp_path / "ready"
    script = (
        "import time; from pathlib import Path; from run_control import FileLock; "
        "lock=FileLock(__import__('sys').argv[1]); lock.acquire(); "
        "Path(__import__('sys').argv[2]).write_text('ready'); time.sleep(30)"
    )
    process = subprocess.Popen([sys.executable, "-c", script, str(lock_path), str(ready)],
                               cwd=Path(__file__).resolve().parents[1],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert ready.exists(), process.poll()
        with pytest.raises(rc.LockUnavailable):
            rc.FileLock(lock_path).acquire()
        process.kill()
        process.wait(timeout=5)
        with rc.FileLock(lock_path):
            pass
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stderr.close()



class FakeStream:
    def __init__(self, chunks):
        self.chunks, self.closed = chunks, False

    def __iter__(self):
        return iter(self.chunks)

    def close(self):
        self.closed = True


def _chunk(text="", reason=None):
    return NS(choices=[NS(delta=NS(content=text), finish_reason=reason)])


def _fake_llm(create):
    llm = object.__new__(RetryingLLM)
    llm.model, llm.temperature, llm.max_tokens, llm.timeout = "offline", .3, 100, 180
    llm._client = NS(chat=NS(completions=NS(create=create)))
    return llm


def test_stream_missing_finish_falls_back_to_validated_nonstream(monkeypatch):
    monkeypatch.setattr("pipeline.agents._BASE_DELAY_S", .001)
    clock = [100.0]
    monkeypatch.setattr(rc.time, "monotonic", lambda: clock[0])
    streams, calls = [], []

    def create(**kwargs):
        calls.append(kwargs)
        clock[0] += .05
        if not kwargs["stream"]:
            return NS(choices=[NS(message=NS(content="complete"), finish_reason="stop")])
        stream = FakeStream([_chunk("partial")])
        streams.append(stream)
        return stream

    llm = _fake_llm(create)
    with rc.budget_scope(rc.Budget(max_llm_calls=2, time_limit_s=5)) as budget:
        assert llm.invoke([]) == "complete"
    assert budget.counts["llm_calls"] == 2
    assert [call["stream"] for call in calls] == [True, False]
    assert all(stream.closed for stream in streams)
    assert 0 < calls[-1]["timeout"] < calls[0]["timeout"] <= 5


def test_nonstream_fallback_remains_finite_and_requires_finish_stop(monkeypatch):
    monkeypatch.setattr("pipeline.agents._BASE_DELAY_S", .001)
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        if kwargs["stream"]:
            return FakeStream([_chunk("partial")])
        return NS(choices=[NS(message=NS(content="partial"), finish_reason=None)])

    with rc.budget_scope(rc.Budget(max_llm_calls=4)) as budget:
        with pytest.raises(IncompleteResponseError, match="finish_missing"):
            _fake_llm(create).invoke([])
    assert budget.counts["llm_calls"] == 4
    assert [call["stream"] for call in calls] == [True, False, False, False]


def test_nonstream_fallback_rejects_gateway_html_without_retry(monkeypatch):
    monkeypatch.setattr("pipeline.agents._BASE_DELAY_S", .001)
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        if kwargs["stream"]:
            return FakeStream([_chunk("not an SSE response")])
        return "<!doctype html><html><body>gateway page</body></html>"

    with rc.budget_scope(rc.Budget(max_llm_calls=10)) as budget:
        with pytest.raises(LLMCallError, match="gateway_html_response") as failure:
            _fake_llm(create).invoke([])
    assert not failure.value.retryable
    assert budget.counts["llm_calls"] == 2
    assert [call["stream"] for call in calls] == [True, False]


def test_budget_stops_network_retry_before_extra_request(monkeypatch):
    monkeypatch.setattr("pipeline.agents._BASE_DELAY_S", .001)
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        raise TimeoutError("transport timed out")

    with rc.budget_scope(rc.Budget(max_llm_calls=1)):
        with pytest.raises(rc.BudgetExceeded, match="max_llm_calls"):
            _fake_llm(create).invoke([])
    assert len(calls) == 1


def test_authentication_error_is_typed_nonretryable_and_redacted():
    calls = []

    class Unauthorized(Exception):
        status_code = 401

    def create(**kwargs):
        calls.append(kwargs)
        raise Unauthorized("secret authentication text")

    with pytest.raises(LLMCallError, match="authentication") as failure:
        _fake_llm(create).invoke([])
    assert not failure.value.retryable
    assert "secret" not in str(failure.value)
    assert len(calls) == 1


def test_unknown_request_failure_without_status_is_retried(monkeypatch):
    monkeypatch.setattr("pipeline.agents._BASE_DELAY_S", .001)
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        if len(calls) < 3:
            raise RuntimeError("gateway closed the response")
        return FakeStream([_chunk("complete"), _chunk(reason="stop")])

    with rc.budget_scope(rc.Budget(max_llm_calls=3)):
        assert _fake_llm(create).invoke([]) == "complete"
    assert len(calls) == 3


def test_transient_retry_limit_is_finite_and_configurable(monkeypatch):
    monkeypatch.setenv("LLM_MAX_ATTEMPTS", "4")
    monkeypatch.setattr("pipeline.agents._BASE_DELAY_S", .001)
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        raise TimeoutError("transport timed out")

    with rc.budget_scope(rc.Budget(max_llm_calls=10)):
        with pytest.raises(LLMCallError, match="transient_http|transport|request_failed"):
            _fake_llm(create).invoke([])
    assert len(calls) == 4


def test_complete_response_is_returned_and_sdk_retries_disabled():
    options = []
    llm = _fake_llm(lambda **kwargs: FakeStream([_chunk("complete"), _chunk(reason="stop")]))
    llm._client.with_options = lambda **kwargs: (options.append(kwargs) or llm._client)
    with rc.budget_scope(rc.Budget(max_llm_calls=1)):
        assert llm.invoke([]) == "complete"
    assert options[0]["max_retries"] == 0


def test_interrupted_upstream_stream_is_retryable(monkeypatch):
    monkeypatch.setattr("pipeline.agents._BASE_DELAY_S", .001)
    calls = []

    class APIError(Exception):
        pass

    class InterruptedStream(FakeStream):
        def __iter__(self):
            raise APIError("Upstream response stream was interrupted")

    def create(**kwargs):
        calls.append(kwargs)
        if len(calls) < 3:
            return InterruptedStream([])
        return FakeStream([_chunk("complete"), _chunk(reason="stop")])

    with rc.budget_scope(rc.Budget(max_llm_calls=3)):
        assert _fake_llm(create).invoke([]) == "complete"
    assert len(calls) == 3


def test_tool_call_invalid_json_is_not_executed(monkeypatch):
    monkeypatch.setattr("pipeline.agents._BASE_DELAY_S", .001)
    message = NS(content=None, tool_calls=[NS(function=NS(arguments='{"unfinished":'))])
    response = NS(choices=[NS(message=message, finish_reason="tool_calls")])
    llm = _fake_llm(lambda **kwargs: response)
    agent = object.__new__(PipelineAgent)
    agent.llm = llm
    with rc.budget_scope(rc.Budget(max_llm_calls=1)):
        with pytest.raises(rc.BudgetExceeded):
            agent._invoke_with_tools([], [], "auto")

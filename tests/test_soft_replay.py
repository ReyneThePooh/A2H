"""Offline contracts for continued replay after post-action content failures."""

from __future__ import annotations

import json

from diff_tester.config import Config
from diff_tester.gate import GateRequest, run_gate
from diff_tester.oracle import PagePairs
from diff_tester.replayer import replay
from diff_tester.schemas import AbstractEvent, StateVector, Trace, save_json


def _state(text: str = "ready", page: str = "pages/Main") -> StateVector:
    return StateVector(page, {text: 1}, {"button": [text]}, {}, {})


def _trace(count: int = 3) -> Trace:
    states = [_state("ready"), _state("after-1"), _state("after-2"), _state("after-3")]
    events = [
        AbstractEvent(
            step=index,
            action="BACK",
            target=None,
            pre_state=states[index - 1],
            pre_state_hash=states[index - 1].hash(),
            post_state=states[index],
        )
        for index in range(1, count + 1)
    ]
    return Trace("soft-replay", "android.app", "harmony.app", events,
                 initial_state=states[0])


class _Device:
    def __init__(self, errors=None):
        self.errors = dict(errors or {})
        self.actions = []

    def reset_app(self):
        return None

    def execute(self, event, node=None):
        self.actions.append(event.action)
        if event.step in self.errors:
            raise self.errors[event.step]

    def wait_stable(self):
        return True

    def ensure_ready(self):
        return None


class _ReplaySequence:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def __call__(self, *args, **kwargs):
        result = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        return result


def _run(monkeypatch, tmp_path, trace, states, device=None):
    samples = iter(states)
    monkeypatch.setattr(
        "diff_tester.replayer.alpha",
        lambda *args, **kwargs: next(samples),
    )
    return replay(
        trace,
        device or _Device(),
        PagePairs({"MainActivity": "pages/Main", "DetailActivity": "pages/Detail"}),
        Config(),
        str(tmp_path),
        collect_artifacts=False,
    )


def test_two_post_action_content_failures_continue_to_last_step(tmp_path, monkeypatch):
    trace = _trace()
    # startup, pre1, post1, pre2, post2, pre3, post3
    result = _run(
        monkeypatch,
        tmp_path,
        trace,
        [
            _state("ready"),
            _state("ready"),
            _state("wrong-1"),
            _state("after-1"),
            _state("wrong-2"),
            _state("after-2"),
            _state("after-3"),
        ],
    )

    assert result.status == "FAIL" and not result.passed
    assert result.stop_reason == ""
    assert result.executed_steps == 3
    assert result.verified_steps == 1
    assert result.soft_failed_steps == [1, 2]
    assert [report.diverged_step for report in result.divergences] == [1, 2]
    assert all(report.kind == "L2_CONTENT" and report.soft_failure
               for report in result.divergences)
    assert [step.step for step in result.steps] == [1, 2, 3]
    assert [step.soft_failed for step in result.steps] == [True, True, False]
    assert result.contract_errors(trace) == []

    restored = type(result).from_dict(json.loads(json.dumps(result.to_dict())))
    assert restored.contract_errors(trace) == []
    assert [report.diverged_step for report in restored.divergences] == [1, 2]


def test_precondition_or_page_failure_remains_hard_stop(tmp_path, monkeypatch):
    trace = _trace()
    # First post-action content mismatch is soft. The next precondition is a
    # wrong page, so step 2 cannot execute and step 3 is never attempted.
    device = _Device()
    result = _run(
        monkeypatch,
        tmp_path,
        trace,
        [
            _state("ready"),
            _state("ready"),
            _state("wrong-1"),
            _state("wrong-page", page="pages/Other"),
        ],
        device,
    )

    assert result.status == "FAIL" and result.stop_reason == "PRECONDITION_MISMATCH"
    assert result.executed_steps == 1
    assert device.actions == ["BACK"]
    assert [report.diverged_step for report in result.divergences] == [1, 2]
    assert result.divergences[0].soft_failure
    assert not result.divergences[1].soft_failure
    assert result.steps[-1].step == 2 and not result.steps[-1].soft_failed
    assert result.contract_errors(trace) == []


def test_trace_result_contract_rejects_softening_a_precondition_failure():
    trace = _trace(1)
    # Build the invalid record directly to ensure the contract, rather than the
    # live replayer, enforces the post-action-only boundary.
    from diff_tester.schemas import DivergenceReport, StepRecord, TraceResult

    record = StepRecord(
        step=1,
        action="BACK",
        verdict_kind="L2_CONTENT",
        phase="precondition",
        cause_class="translation",
        action_executed=True,
        verified=True,
        status="FAIL",
        soft_failed=True,
        failure_detail={"missing_texts": ["x"]},
    )
    report = DivergenceReport(
        trace_id=trace.trace_id,
        diverged_step=1,
        kind="L2_CONTENT",
        detail={"phase": "precondition", "cause_class": "translation",
                "action_executed": True},
        android_state=trace.events[0].pre_state,
        harmony_state=_state("wrong"),
        phase="precondition",
        cause_class="translation",
        action_executed=True,
        soft_failure=True,
    )
    result = TraceResult(
        trace_id=trace.trace_id,
        total_steps=1,
        executed_steps=1,
        passed=False,
        first_divergence_step=1,
        divergence=report,
        divergences=[report],
        steps=[record],
        status="FAIL",
        verified_steps=0,
        stop_reason="",
        soft_failed_steps=[1],
    )
    assert any("only post-action" in error for error in result.contract_errors(trace))


def test_trace_result_contract_rejects_dropped_soft_divergence_report(
        tmp_path, monkeypatch):
    trace = _trace(2)
    result = _run(
        monkeypatch,
        tmp_path,
        trace,
        [
            _state("ready"),
            _state("ready"),
            _state("wrong-1"),
            _state("after-1"),
            _state("wrong-2"),
        ],
    )

    assert result.soft_failed_steps == [1, 2]
    result.divergences = result.divergences[:1]
    errors = result.contract_errors(trace)
    assert any("soft-failed step 2" in error
               and "divergence report" in error for error in errors)


def test_trace_result_contract_rejects_inconclusive_soft_only_without_terminal(
        tmp_path, monkeypatch):
    trace = _trace(2)
    result = _run(
        monkeypatch,
        tmp_path,
        trace,
        [
            _state("ready"),
            _state("ready"),
            _state("wrong-1"),
            _state("after-1"),
            _state("wrong-2"),
        ],
    )

    result.status = "INCONCLUSIVE"
    result.stop_reason = ""
    assert any("lacks a terminal divergence" in error
               for error in result.contract_errors(trace))


def test_flaky_downgrade_keeps_complete_soft_only_result_valid(
        tmp_path, monkeypatch):
    from diff_tester.gate.gate_runner import _flaky_inconclusive_result

    trace = _trace(2)
    result = _run(
        monkeypatch,
        tmp_path,
        trace,
        [
            _state("ready"),
            _state("ready"),
            _state("wrong-1"),
            _state("after-1"),
            _state("wrong-2"),
            _state("after-2"),
        ],
    )

    flaky = _flaky_inconclusive_result(result)
    assert flaky.status == "INCONCLUSIVE"
    assert flaky.stop_reason == "FLAKY"
    assert flaky.contract_errors(trace) == []


def test_gate_emits_one_report_per_soft_failure_and_confirms_ordered_group(
        tmp_path, monkeypatch):
    trace = _trace()
    seed_root = tmp_path / "workspace" / "seeds"
    seed_root.mkdir(parents=True)
    save_json(trace.to_dict(), str(seed_root / "soft-replay.json"))

    states = [
        _state("ready"), _state("ready"), _state("wrong-1"),
        _state("after-1"), _state("wrong-2"), _state("after-2"),
        _state("after-3"),
    ]
    first = _run(monkeypatch, tmp_path / "first", trace, states)
    confirm = _run(monkeypatch, tmp_path / "confirm", trace, states)
    sequence = _ReplaySequence([first, confirm])

    gate = run_gate(
        GateRequest(bundle="harmony.app", workspace=str(tmp_path / "workspace")),
        harmony=_Device(),
        replay_fn=sequence,
    )

    assert gate.status == "FAIL"
    assert sequence.calls == 2
    assert [report.diverged_step for report in gate.reports] == [1, 2]
    assert all(report.evidence["confirmed"] is True for report in gate.reports)
    assert all(len(report.evidence["confirmation_fingerprints"]["initial"]) == 2
               for report in gate.reports)
    assert all(report.evidence["confirmation_fingerprints"]["initial"]
               == report.evidence["confirmation_fingerprints"]["replay"]
               for report in gate.reports)

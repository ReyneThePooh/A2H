"""Offline regressions for the direct build/replay/repair loop."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from diff_tester.gate.gate_runner import GateResult
from diff_tester.gate.repair_report import RepairReport
from pipeline import functional_fixer as functional
from pipeline.artifacts import project_source_fingerprint
from pipeline.repair_ledger import aggregate_failure_identity
from run_control import BudgetExceeded
from test_artifact_contract import make_project


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    source = root / "entry/src/main/ets/pages/MainPage.ets"
    source.parent.mkdir(parents=True)
    source.write_text("@Entry\n@Component\nstruct MainPage { build() { Text('old') } }\n",
                      encoding="utf-8")
    return SimpleNamespace(root=root, source=source, workspace=tmp_path / "gate",
                           sync=tmp_path / "sync", seeds=tmp_path / "seeds")


def report(project, *, trace="trace-a", step=0, kind="STARTUP_STATE_MISMATCH",
           cause="translation", located=True, confirmed=True):
    return RepairReport(
        trace_id=trace, trace_intent="Open details", diverged_step=step,
        failure_type=kind, abstract_event={"action": "click", "target": {"text": "Details"}},
        expected={"page": "Main" if step == 0 else "Detail"}, actual={"page": "Wrong"},
        suspect_files=[project.source.relative_to(project.root).as_posix()] if located else [],
        phase="startup" if step == 0 else "compare", cause_class=cause,
        action_executed=step > 0,
        evidence={"confirmed": confirmed},
    )


def gate(*reports, passed=False, status=None, reason="", verified_steps=0,
         traces=None, flaky=None, skipped=None):
    trace_ids = list(traces if traces is not None else
                     (dict.fromkeys(r.trace_id for r in reports) or ["trace-a"]))
    trace_status = {name: "PASS" if passed else "FAIL" for name in trace_ids}
    trace_status.update({name: "NOT_RUN" for name in (skipped or [])})
    return GateResult(
        passed=passed, status=status or ("PASS" if passed else "FAIL"),
        stop_reason=reason, reports=list(reports), ran_traces=trace_ids,
        skipped_traces=list(skipped or []), flaky=flaky or [], elapsed_s=0.01,
        trace_status=trace_status, trace_passed={name: passed for name in trace_ids},
        metrics={"verified_steps": verified_steps, "per_trace": [
            {"trace_id": name, "verified_steps": verified_steps} for name in trace_ids]},
    )


def wire(monkeypatch, project, results, *, current=True, builds=None, rounds=3,
         build_mutations=None):
    events, build_sources, replay_sources, received, gate_requests = [], [], [], [], []
    remaining_gates = iter(results)
    remaining_builds = iter(builds or [True] * rounds)
    remaining_mutations = iter(build_mutations or [None] * rounds)

    def prepare(*args, **kwargs):
        events.append("prepare")

    def replay(*args, **kwargs):
        assert Path(args[0]).resolve() == project.root.resolve()
        assert kwargs["full_replay"] is True
        assert kwargs["confirm_failures"] is True
        gate_requests.append(kwargs.copy())
        replay_sources.append(project.source.read_text(encoding="utf-8"))
        events.append("gate")
        value = next(remaining_gates)
        if isinstance(value, BaseException):
            raise value
        return value

    real_build = functional.BuildFixLoop

    class FakeBuild:
        _extract_code = staticmethod(real_build._extract_code)
        _local_dependency_context = staticmethod(real_build._local_dependency_context)
        _validate_fixed = staticmethod(real_build._validate_fixed)

        def __init__(self, *args, **kwargs):
            pass

        def run(self, root, **kwargs):
            assert Path(root).resolve() == project.root.resolve()
            build_sources.append(project.source.read_text(encoding="utf-8"))
            events.append("build")
            mutation = next(remaining_mutations)
            if mutation:
                project.source.write_text(
                    project.source.read_text(encoding="utf-8") + mutation,
                    encoding="utf-8",
                )
            return {"success": next(remaining_builds)}

    monkeypatch.setattr(functional, "prepare_workspace", prepare)
    monkeypatch.setattr(functional, "run_diff_gate", replay)
    monkeypatch.setattr(functional, "BuildFixLoop", FakeBuild)
    monkeypatch.setattr(functional, "build_is_current", lambda root: current)
    loop = functional.FunctionalFixLoop(llm=SimpleNamespace(invoke=Mock()),
                                        max_gate_rounds=rounds, build_fix_rounds=1)

    def repair(project_dir, sync_dir, plan, reports, prev_signatures, prev_diffs):
        assert Path(project_dir).resolve() == project.root.resolve()
        received.append(list(reports))
        events.append("repair")
        project.source.write_text(project.source.read_text(encoding="utf-8") +
                                  f"// repair {len(received)}\n", encoding="utf-8")
        return True, {r.signature(): f"diff {len(received)}" for r in reports}

    monkeypatch.setattr(loop, "_fix_reports", repair)
    return SimpleNamespace(loop=loop, events=events, builds=build_sources,
                           replays=replay_sources, repairs=received,
                           gate_requests=gate_requests)


def run(ctx, project):
    return ctx.loop.run(project.root, project.sync, project.workspace,
                        seeds_dir=str(project.seeds), bundle="demo.weather",
                        device="fake-device", hdc_path="fake-hdc", time_budget_s=30)


def assert_saved_result(project, result):
    saved = json.loads((project.workspace / "functional_result.json").read_text(encoding="utf-8"))
    assert saved == result
    state = json.loads((project.workspace / "functional_state.json").read_text(encoding="utf-8"))
    assert state["status"] not in {"running", "building", "replaying", "repairing"}
    return state


def test_code_fence_parser_preserves_first_source_line():
    code = "@Entry\n@Component\nstruct MainPage {}"

    assert functional.BuildFixLoop._extract_code(
        f"```typescript\n{code}\n```"
    ) == code


def test_zero_verified_steps_keeps_repair_and_uses_latest_deeper_report(project, monkeypatch):
    startup = report(project)
    deeper = report(project, step=1, kind="WRONG_PAGE")
    ctx = wire(monkeypatch, project, [gate(startup), gate(deeper),
                                     gate(passed=True, verified_steps=12)], rounds=3)

    result = run(ctx, project)

    assert result["success"]
    assert result["repair_attempts"] == 2
    assert result["gate_calls"] == 3
    assert len(result["gate_history"]) == 3
    assert ctx.repairs == [[startup], [deeper]]
    assert "// repair 1" in ctx.builds[1]
    assert "// repair 1" in ctx.replays[2]
    assert "// repair 2" in project.source.read_text(encoding="utf-8")
    assert not (project.workspace / "repair" / "transactions").exists()
    assert_saved_result(project, result)


def test_last_allowed_repair_always_builds_and_replays(project, monkeypatch):
    ctx = wire(monkeypatch, project, [gate(report(project)), gate(passed=True)], rounds=2)

    result = run(ctx, project)

    assert result["success"]
    assert result["gate_calls"] == 2
    assert result["repair_attempts"] == 1
    assert [event for event in ctx.events if event != "prepare"] == [
        "gate", "repair", "build", "gate"]
    assert_saved_result(project, result)


def test_focused_replay_must_pass_before_full_validation(project, monkeypatch):
    failure = report(project, trace="trace-a")
    initial = gate(failure, traces=["trace-a", "trace-b"])
    initial.trace_status["trace-b"] = "PASS"
    initial.trace_passed["trace-b"] = True
    ctx = wire(monkeypatch, project, [
        initial,
        gate(passed=True, traces=["trace-a"], skipped=["trace-b"]),
        gate(passed=True, traces=["trace-a", "trace-b"]),
    ], rounds=2)

    result = run(ctx, project)

    assert result["success"]
    assert result["repair_attempts"] == 1
    assert result["repair_iterations"] == 1
    assert result["gate_calls"] == 3
    assert result["full_gate_calls"] == 2
    assert result["diagnostic_gate_calls"] == 1
    assert result["rounds"] == 3
    assert result["rounds_semantics"] == "gate_calls"
    assert [request["trace_ids"] for request in ctx.gate_requests] == [
        None, ["trace-a"], None]
    assert [request["install"] for request in ctx.gate_requests] == [True, True, True]


def test_full_validation_failure_overrides_diagnostic_pass(project, monkeypatch):
    failure = report(project, trace="trace-a")
    initial = gate(failure, traces=["trace-a", "trace-b"])
    initial.trace_status["trace-b"] = "PASS"
    initial.trace_passed["trace-b"] = True
    focused = gate(passed=True, traces=["trace-a"], skipped=["trace-b"])
    validation = gate(failure, traces=["trace-a", "trace-b"])
    validation.trace_status["trace-b"] = "PASS"
    validation.trace_passed["trace-b"] = True
    ctx = wire(monkeypatch, project, [initial, focused, validation], rounds=2)

    result = run(ctx, project)

    assert result["stop_reason"] == "ROUND_LIMIT"
    ledger = json.loads(
        (project.workspace / "repair_ledger.json").read_text(encoding="utf-8")
    )
    attempt, candidate, diagnostic, authoritative = ledger["events"]
    assert [event["event"] for event in ledger["events"]] == [
        "repair_attempt", "candidate_ready", "gate_result", "gate_result"
    ]
    assert diagnostic["attempt_id"] == authoritative["attempt_id"] == attempt["attempt_id"]
    assert diagnostic["scope"] == "diagnostic"
    assert diagnostic["decision"]["action"] == "VALIDATE_FULL"
    assert authoritative["scope"] == "full_validation"
    assert authoritative["decision"]["action"] == "REPAIR"
    assert authoritative["gate"]["passed"] is False
    assert diagnostic["tested_source"] == authoritative["tested_source"]
    assert authoritative["tested_source"] == candidate["tested_source"]


def test_resume_after_diagnostic_pass_continues_full_validation(project, monkeypatch):
    failure = report(project, trace="trace-a")
    initial = gate(failure, traces=["trace-a", "trace-b"])
    initial.trace_status["trace-b"] = "PASS"
    initial.trace_passed["trace-b"] = True
    focused = gate(passed=True, traces=["trace-a"], skipped=["trace-b"])
    interrupted = wire(
        monkeypatch,
        project,
        [initial, focused, KeyboardInterrupt()],
        rounds=2,
    )

    with pytest.raises(KeyboardInterrupt):
        run(interrupted, project)

    resumed = wire(monkeypatch, project, [gate(passed=True)], rounds=1)
    result = run(resumed, project)

    assert result["success"]
    assert resumed.gate_requests[0]["trace_ids"] is None
    ledger = json.loads(
        (project.workspace / "repair_ledger.json").read_text(encoding="utf-8")
    )
    gate_events = [event for event in ledger["events"]
                   if event["event"] == "gate_result"]
    assert [event["scope"] for event in gate_events] == [
        "diagnostic", "full_validation"
    ]
    assert gate_events[-1]["decision"]["action"] == "PASS"


def test_focused_replay_still_blocks_unattempted_selected_trace(project, monkeypatch):
    focused = gate(passed=True, traces=["trace-a"], skipped=["trace-c"])
    focused.trace_status["trace-b"] = "NOT_RUN"
    ctx = wire(monkeypatch, project, [focused], rounds=1)

    result = run(ctx, project)

    assert not result["success"]
    assert result["stop_reason"] == "INCOMPLETE_REPLAY"
    assert result["gate_calls"] == 1


def test_manifest_run_ignores_broken_stale_plan(tmp_path, monkeypatch):
    root, data = make_project(tmp_path)
    project = SimpleNamespace(root=root, source=root / data["outputs"][0]["path"],
                              workspace=tmp_path / "gate", sync=None, seeds=tmp_path / "seeds")
    plan = tmp_path / "translation_plan.json"
    plan.write_text("broken stale translation plan", encoding="utf-8")
    ctx = wire(monkeypatch, project, [gate(passed=True)], rounds=1)

    result = ctx.loop.run(root, None, project.workspace, plan_path=plan)

    assert result["success"]
    assert result["gate_calls"] == 1
    assert ctx.repairs == []
    assert_saved_result(project, result)


def test_round_limit_never_leaves_an_unvalidated_functional_patch(project, monkeypatch):
    ctx = wire(monkeypatch, project, [gate(report(project))], rounds=1)

    result = run(ctx, project)

    assert not result["success"]
    assert result["repair_attempts"] == 0
    assert result["gate_calls"] == 1
    assert ctx.repairs == []
    assert ctx.builds == []
    assert_saved_result(project, result)


def test_failed_rebuild_keeps_source_and_never_replays_old_hap(project, monkeypatch):
    ctx = wire(monkeypatch, project, [gate(report(project))], builds=[False], rounds=2)

    result = run(ctx, project)

    assert not result["success"]
    assert result["stop_reason"] == "BUILD_FAILED"
    assert result["gate_calls"] == 1
    assert len(ctx.builds) == 1
    assert "// repair 1" in project.source.read_text(encoding="utf-8")
    assert_saved_result(project, result)

    resumed = wire(monkeypatch, project, [gate(passed=True)], current=False, rounds=1)
    continued = run(resumed, project)
    assert continued["success"]
    assert "// repair 1" in resumed.builds[0]
    assert resumed.events.index("build") < resumed.events.index("gate")
    assert_saved_result(project, continued)


def test_initial_build_failure_prevents_any_replay_or_functional_repair(project, monkeypatch):
    ctx = wire(monkeypatch, project, [], current=False, builds=[False])

    result = run(ctx, project)

    assert not result["success"]
    assert result["stop_reason"] == "BUILD_FAILED"
    assert result["gate_calls"] == 0
    assert ctx.repairs == []
    assert ctx.replays == []
    assert_saved_result(project, result)


@pytest.mark.parametrize("kind,cause", [
    ("INFRA_ERROR", "infrastructure"), ("BASELINE_INVALID", "baseline"),
    ("INCONCLUSIVE", "unknown"), ("FLAKY", "translation"),
])
def test_unverified_failure_never_invokes_functional_model(project, monkeypatch, kind, cause):
    failed = gate(report(project, kind=kind, cause=cause), status="INCONCLUSIVE", reason=kind,
                  flaky=["trace-a"] if kind == "FLAKY" else None)
    ctx = wire(monkeypatch, project, [failed])

    result = run(ctx, project)

    assert not result["success"]
    assert result["stop_reason"] == kind
    assert result["repair_attempts"] == 0
    assert ctx.repairs == []
    ctx.loop.llm.invoke.assert_not_called()
    assert_saved_result(project, result)


def test_unconfirmed_translation_failure_never_invokes_model(project, monkeypatch):
    failed = gate(report(project, confirmed=False))
    ctx = wire(monkeypatch, project, [failed])

    result = run(ctx, project)

    assert result["stop_reason"] == "UNCONFIRMED_FAILURE"
    assert result["repair_attempts"] == 0
    assert ctx.repairs == []
    ctx.loop.llm.invoke.assert_not_called()


def test_platform_mediation_failure_never_invokes_model(project, monkeypatch):
    failed = gate(report(project, kind="PLATFORM_MEDIATION", cause="platform"))
    ctx = wire(monkeypatch, project, [failed])

    result = run(ctx, project)

    assert result["stop_reason"] == "PLATFORM_MEDIATION"
    assert result["repair_attempts"] == 0
    assert ctx.repairs == []
    ctx.loop.llm.invoke.assert_not_called()


@pytest.mark.parametrize("cause,expected", [
    ("baseline", "BASELINE_INVALID"),
    ("platform", "PLATFORM_MEDIATION"),
])
def test_external_protocol_inconclusive_keeps_typed_stop_reason(
        project, monkeypatch, cause, expected):
    protocol = report(
        project,
        kind="EXTERNAL_PROTOCOL",
        cause=cause,
        confirmed=False,
    )
    failed = gate(
        protocol,
        status="INCONCLUSIVE",
        reason="EXTERNAL_PROTOCOL",
    )
    ctx = wire(monkeypatch, project, [failed])

    result = run(ctx, project)

    assert result["stop_reason"] == expected
    assert result["repair_attempts"] == 0
    ctx.loop.llm.invoke.assert_not_called()


def test_missing_report_for_selected_failure_never_repairs(project, monkeypatch):
    failed = gate(report(project, trace="trace-a"),
                  traces=["trace-a", "trace-b"])
    ctx = wire(monkeypatch, project, [failed])

    result = run(ctx, project)

    assert result["stop_reason"] == "NEEDS_EVIDENCE"
    assert result["repair_attempts"] == 0
    ctx.loop.llm.invoke.assert_not_called()


def test_selected_trace_claimed_pass_without_running_is_incomplete(project, monkeypatch):
    invalid = gate(passed=True, traces=["trace-a", "trace-b"])
    invalid.ran_traces = ["trace-a"]
    ctx = wire(monkeypatch, project, [invalid])

    result = run(ctx, project)

    assert result["stop_reason"] == "INCOMPLETE_REPLAY"
    assert result["repair_attempts"] == 0


def test_empty_gate_cannot_be_success_even_if_backend_claims_pass(project, monkeypatch):
    ctx = wire(monkeypatch, project, [gate(passed=True, traces=[])])

    result = run(ctx, project)

    assert not result["success"]
    assert result["stop_reason"] == "EMPTY_TEST_SET"
    assert ctx.repairs == []
    assert_saved_result(project, result)


@pytest.mark.parametrize("error,reason", [
    (RuntimeError("device disappeared"), "ERROR"),
    (KeyboardInterrupt(), "INTERRUPTED"),
    (BudgetExceeded("time_budget"), "BUDGET_EXHAUSTED"),
])
def test_gate_exception_records_terminal_result(project, monkeypatch, error, reason):
    ctx = wire(monkeypatch, project, [error])

    with pytest.raises(type(error)):
        run(ctx, project)

    result = json.loads((project.workspace / "functional_result.json").read_text(encoding="utf-8"))
    assert result["success"] is False
    assert result["stop_reason"] == reason
    assert_saved_result(project, result)


def test_multiple_traces_for_same_file_produce_one_combined_repair(project, monkeypatch):
    first = report(project, trace="weather-now")
    first.trace_intent = "Current weather"
    second = report(project, trace="weather-next", step=1, kind="WRONG_PAGE")
    second.trace_intent = "Next day weather"
    loop = functional.FunctionalFixLoop(llm=SimpleNamespace(invoke=Mock()))
    fix = Mock(return_value="one combined diff")
    monkeypatch.setattr(loop, "_fix_file", fix)

    changed, diffs = loop._fix_reports(project.root, None, {"units": []},
                                       [first, second], set(), {})

    assert changed
    fix.assert_called_once()
    path, prompt, _repeat = fix.call_args.args
    assert path == project.source
    assert "Current weather" in prompt
    assert "Next day weather" in prompt
    assert set(diffs) == {first.signature(), second.signature()}


def test_multi_file_model_failure_leaves_every_source_unchanged(project):
    second_path = project.source.with_name("SecondPage.ets")
    second_path.write_text(
        "@Component\nstruct SecondPage { build() { Text('second-old') } }\n",
        encoding="utf-8",
    )
    first_source = project.source.read_text(encoding="utf-8")
    second_source = second_path.read_text(encoding="utf-8")
    second_report = report(project, trace="trace-b")
    second_report.suspect_files = [second_path.relative_to(project.root).as_posix()]
    calls = 0

    def invoke(_messages):
        nonlocal calls
        calls += 1
        assert project.source.read_text(encoding="utf-8") == first_source
        assert second_path.read_text(encoding="utf-8") == second_source
        if calls == 1:
            return "```typescript\n" + first_source.replace("'old'", "'first-new'") + "```"
        raise RuntimeError("second proposal failed")

    loop = functional.FunctionalFixLoop(llm=SimpleNamespace(invoke=invoke))

    with pytest.raises(RuntimeError, match="second proposal failed"):
        loop._fix_reports(
            project.root, None, {"units": []},
            [report(project, trace="trace-a"), second_report], set(), {},
        )

    assert project.source.read_text(encoding="utf-8") == first_source
    assert second_path.read_text(encoding="utf-8") == second_source
    assert not project.source.with_suffix(".ets.funcbak").exists()
    assert not second_path.with_suffix(".ets.funcbak").exists()


def test_multi_file_repair_commits_only_after_all_proposals_validate(project):
    second_path = project.source.with_name("SecondPage.ets")
    second_path.write_text(
        "@Component\nstruct SecondPage { build() { Text('second-old') } }\n",
        encoding="utf-8",
    )
    first_source = project.source.read_text(encoding="utf-8")
    second_source = second_path.read_text(encoding="utf-8")
    first_fixed = first_source.replace("'old'", "'first-new'")
    second_fixed = second_source.replace("'second-old'", "'second-new'")
    second_report = report(project, trace="trace-b")
    second_report.suspect_files = [second_path.relative_to(project.root).as_posix()]
    responses = iter((first_fixed, second_fixed))

    def invoke(_messages):
        assert project.source.read_text(encoding="utf-8") == first_source
        assert second_path.read_text(encoding="utf-8") == second_source
        return "```typescript\n" + next(responses) + "```"

    loop = functional.FunctionalFixLoop(llm=SimpleNamespace(invoke=invoke))

    outcome = loop._fix_reports(
        project.root, None, {"units": []},
        [report(project, trace="trace-a"), second_report], set(), {},
    )

    assert outcome.changed and outcome.files_changed == 2
    assert project.source.read_text(encoding="utf-8").rstrip() == first_fixed.rstrip()
    assert second_path.read_text(encoding="utf-8").rstrip() == second_fixed.rstrip()
    assert project.source.with_suffix(".ets.funcbak").read_text(encoding="utf-8") == first_source
    assert second_path.with_suffix(".ets.funcbak").read_text(encoding="utf-8") == second_source


def test_multi_file_commit_failure_rolls_back_first_write(project, monkeypatch):
    second_path = project.source.with_name("SecondPage.ets")
    second_path.write_text(
        "@Component\nstruct SecondPage { build() { Text('second-old') } }\n",
        encoding="utf-8",
    )
    first_source = project.source.read_text(encoding="utf-8")
    second_source = second_path.read_text(encoding="utf-8")
    second_report = report(project, trace="trace-b")
    second_report.suspect_files = [second_path.relative_to(project.root).as_posix()]
    responses = iter((
        first_source.replace("'old'", "'first-new'"),
        second_source.replace("'second-old'", "'second-new'"),
    ))
    loop = functional.FunctionalFixLoop(llm=SimpleNamespace(
        invoke=lambda _messages: "```typescript\n" + next(responses) + "```"
    ))
    real_atomic_write = functional.atomic_write
    writes = 0

    def fail_second_write(path, data):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("injected commit failure")
        return real_atomic_write(path, data)

    monkeypatch.setattr(functional, "atomic_write", fail_second_write)

    with pytest.raises(OSError, match="injected commit failure"):
        loop._fix_reports(
            project.root, None, {"units": []},
            [report(project, trace="trace-a"), second_report], set(), {},
        )

    assert project.source.read_text(encoding="utf-8") == first_source
    assert second_path.read_text(encoding="utf-8") == second_source
    assert not project.source.with_suffix(".ets.funcbak").exists()
    assert not second_path.with_suffix(".ets.funcbak").exists()


def test_ledger_append_failure_rolls_back_functional_patch(project, monkeypatch):
    failure = report(project)
    original = project.source.read_bytes()
    fixed = original.decode("utf-8").replace("Text('old')", "Text('fixed')")
    ctx = wire(monkeypatch, project, [gate(failure)], rounds=2)
    _restore_real_report_fixer(ctx)
    ctx.loop.llm.invoke.return_value = f"```typescript\n{fixed}\n```"

    def reject_attempt(_self, **_kwargs):
        raise functional.RepairLedgerError("injected ledger failure")

    monkeypatch.setattr(functional.RepairLedger, "append_attempt", reject_attempt)

    result = run(ctx, project)

    assert result["stop_reason"] == "REPAIR_LEDGER_INVALID"
    assert project.source.read_bytes() == original
    assert not project.source.with_suffix(".ets.funcbak").exists()
    assert not (project.workspace / "repair_transactions").exists()


def test_functional_repair_updates_sync_source_and_both_manifests(
        tmp_path, monkeypatch):
    packaged, _ = make_project(tmp_path / "packaged")
    generated, _ = make_project(tmp_path / "generated")
    source = packaged / "entry/src/main/ets/pages/Renamed.ets"
    sync_source = generated / "entry/src/main/ets/pages/Renamed.ets"
    project = SimpleNamespace(root=packaged, source=source)
    fixed = source.read_text(encoding="utf-8").replace(
        "build() {}", "build() { Text('fixed') }"
    )
    loop = functional.FunctionalFixLoop(
        llm=SimpleNamespace(invoke=Mock(return_value=f"```typescript\n{fixed}\n```"))
    )

    outcome = loop._fix_reports(
        packaged, generated, {"units": []}, [report(project)], set(), {}
    )

    from pipeline.artifacts import validate_project_contract
    assert outcome.changed
    assert source.read_text(encoding="utf-8").rstrip() == fixed.rstrip()
    assert sync_source.read_text(encoding="utf-8").rstrip() == fixed.rstrip()
    assert validate_project_contract(packaged) == []
    assert validate_project_contract(generated) == []


@pytest.mark.parametrize("recorded", [False, True])
def test_functional_transaction_recovery_follows_ledger_commit(
        tmp_path, recorded):
    from pipeline.artifacts import (load_artifact_manifest,
                                    validate_project_contract)

    packaged, _ = make_project(tmp_path / "packaged")
    generated, _ = make_project(tmp_path / "generated")
    source = packaged / "entry/src/main/ets/pages/Renamed.ets"
    sync_source = generated / "entry/src/main/ets/pages/Renamed.ets"
    original = source.read_bytes()
    sync_original = sync_source.read_bytes()
    packaged_manifest_before = (packaged / "translation_manifest.json").read_bytes()
    generated_manifest_before = (generated / "translation_manifest.json").read_bytes()
    replacement = original.decode("utf-8").replace(
        "build() {}", "build() { Text('fixed') }"
    ).encode("utf-8")
    work = tmp_path / "work"
    loop = functional.FunctionalFixLoop(llm=object())
    loop._transaction_dir = work / "repair_transactions"
    loop._transaction_roots = [packaged.resolve(), generated.resolve()]
    source_before = project_source_fingerprint(packaged)
    ledger = functional.RepairLedger(
        work,
        project_identity=functional.stable_project_identity(packaged),
    )
    transaction_id = loop._commit_file_repairs(
        packaged.resolve(), generated.resolve(),
        load_artifact_manifest(packaged),
        [(source.resolve(), functional.FileRepairOutcome(
            "PATCH_APPLIED", diff="diff", replacement=replacement,
            original=original,
        ))],
    )

    if recorded:
        identity, fingerprints = aggregate_failure_identity(["failure"])
        context = functional.repair_context_fingerprint(
            packaged, None, {"units": []}, "test-model", workspace=work
        )
        ledger.append_attempt(
            source_before=source_before,
            source_after_patch=project_source_fingerprint(packaged),
            failure_identity=identity,
            failure_fingerprints=fingerprints,
            repair_context=context,
            outcome={
                "code": "PATCH_APPLIED",
                "details": {"transaction_id": transaction_id},
            },
            diffs={"failure": "diff"},
        )

    actions = functional.recover_file_transactions(
        loop._transaction_dir,
        owner="functional_repair",
        roots=loop._transaction_roots,
        keep_committed=ledger.has_transaction,
    )

    assert actions == [(
        transaction_id, "kept" if recorded else "rolled_back"
    )]
    backup = source.with_suffix(".ets.funcbak")
    if recorded:
        assert source.read_bytes() == replacement
        assert sync_source.read_bytes() == replacement
        assert backup.read_bytes() == original
        assert validate_project_contract(packaged) == []
        assert validate_project_contract(generated) == []
    else:
        assert source.read_bytes() == original
        assert sync_source.read_bytes() == sync_original
        assert not backup.exists()
        assert (packaged / "translation_manifest.json").read_bytes() == packaged_manifest_before
        assert (generated / "translation_manifest.json").read_bytes() == generated_manifest_before
    assert not loop._transaction_dir.exists()


def test_missing_location_never_scans_unrelated_pages(project, monkeypatch):
    other = project.source.with_name("UnrelatedPage.ets")
    other.write_text("struct UnrelatedPage {}", encoding="utf-8")
    loop = functional.FunctionalFixLoop(llm=SimpleNamespace(invoke=Mock()))
    fix = Mock(return_value="must not run")
    monkeypatch.setattr(loop, "_fix_file", fix)

    changed, diffs = loop._fix_reports(project.root, None, {"units": []},
                                       [report(project, located=False)], set(), {})

    assert not changed
    assert diffs == {}
    fix.assert_not_called()
    loop.llm.invoke.assert_not_called()


@pytest.mark.parametrize("cause,confirmed", [
    ("platform", True),
    ("translation", False),
])
def test_fix_reports_defensively_rejects_ineligible_evidence(
        project, monkeypatch, cause, confirmed):
    loop = functional.FunctionalFixLoop(llm=SimpleNamespace(invoke=Mock()))
    fix = Mock(return_value="must not run")
    monkeypatch.setattr(loop, "_fix_file", fix)

    changed, diffs = loop._fix_reports(
        project.root, None, {"units": []},
        [report(project, cause=cause, confirmed=confirmed)], set(), {},
    )

    assert not changed and diffs == {}
    fix.assert_not_called()
    loop.llm.invoke.assert_not_called()


def test_unknown_translation_failure_type_is_not_automatically_repaired(
        project, monkeypatch):
    unknown = report(project, kind="FUTURE_FAILURE")
    loop = functional.FunctionalFixLoop(llm=SimpleNamespace(invoke=Mock()))
    fix = Mock(return_value="must not run")
    monkeypatch.setattr(loop, "_fix_file", fix)

    changed, diffs = loop._fix_reports(
        project.root, None, {"units": []}, [unknown], set(), {})

    assert not changed and diffs == {}
    fix.assert_not_called()


def test_unknown_translation_failure_type_stops_at_gate_decision(
        project, monkeypatch):
    unknown = report(project, kind="FUTURE_FAILURE")
    ctx = wire(monkeypatch, project, [gate(unknown)])

    result = run(ctx, project)

    assert result["stop_reason"] == "NEEDS_EVIDENCE"
    assert result["repair_attempts"] == 0
    assert ctx.repairs == []


def test_mixed_mapped_and_unmapped_reports_fail_closed(project, monkeypatch):
    mapped = report(project, trace="mapped")
    unmapped = report(project, trace="unmapped", located=False)
    loop = functional.FunctionalFixLoop(llm=SimpleNamespace(invoke=Mock()))
    fix = Mock(return_value="must not run")
    monkeypatch.setattr(loop, "_fix_file", fix)

    changed, diffs = loop._fix_reports(
        project.root, None, {"units": []}, [mapped, unmapped], set(), {})

    assert not changed and diffs == {}
    fix.assert_not_called()


def test_invalid_suspect_file_falls_back_to_manifest_page_mapping(
        tmp_path, monkeypatch):
    root, _manifest = make_project(tmp_path)
    page = next((root / "entry/src/main/ets/pages").glob("*.ets"))
    failure = RepairReport(
        trace_id="fallback", trace_intent="Open start", diverged_step=1,
        failure_type="CONTENT_LOSS",
        abstract_event={"action": "CLICK", "target": {"text": "Open"}},
        expected={"page": ".Start"}, actual={"page": "pages/Renamed"},
        suspect_files=["entry/src/main/ets/pages/Missing.ets"],
        phase="compare", cause_class="translation", action_executed=True,
        evidence={"confirmed": True, "pre_state": {"page": ".Start"}},
    )
    loop = functional.FunctionalFixLoop(llm=SimpleNamespace(invoke=Mock()))
    fix = Mock(return_value="manifest fallback diff")
    monkeypatch.setattr(loop, "_fix_file", fix)

    changed, _diffs = loop._fix_reports(
        root, None, {"units": []}, [failure], set(), {})

    assert changed
    fix.assert_called_once()
    assert fix.call_args.args[0] == page.resolve()


def test_manifest_page_mapping_avoids_repairing_entire_merged_unit(tmp_path, monkeypatch):
    root, _manifest = make_project(tmp_path)
    page = next((root / "entry/src/main/ets/pages").glob("*.ets"))
    failure = RepairReport(
        trace_id="open-start", trace_intent="Open start page", diverged_step=1,
        failure_type="CONTENT_LOSS",
        abstract_event={"action": "CLICK", "target": {"text": "Open"}},
        expected={"page": ".Start"}, actual={"page": "pages/Renamed"},
        suspect_units=["large-merged-unit"], phase="compare",
        cause_class="translation", action_executed=True,
        evidence={"confirmed": True, "pre_state": {"page": ".Start"}},
    )
    loop = functional.FunctionalFixLoop(llm=SimpleNamespace(invoke=Mock()))
    fix = Mock(return_value="targeted page diff")
    monkeypatch.setattr(loop, "_fix_file", fix)

    changed, _diffs = loop._fix_reports(
        root, None, {"units": []}, [failure], set(), {})

    assert changed
    fix.assert_called_once()
    assert fix.call_args.args[0] == page.resolve()


def _restore_real_report_fixer(ctx):
    ctx.loop._fix_reports = functional.FunctionalFixLoop._fix_reports.__get__(ctx.loop)


def test_repair_ledger_links_patch_to_subsequent_gate(project, monkeypatch):
    failure = report(project)
    source_before = project_source_fingerprint(project.root)
    expected_identity, expected_fingerprints = aggregate_failure_identity(
        [failure.confirmation_fingerprint]
    )
    ctx = wire(monkeypatch, project, [gate(failure), gate(passed=True)], rounds=2)

    result = run(ctx, project)

    ledger = json.loads(
        (project.workspace / "repair_ledger.json").read_text(encoding="utf-8")
    )
    attempt, candidate, validation = ledger["events"]
    assert result["success"]
    assert attempt["event"] == "repair_attempt"
    assert attempt["failure_identity"] == expected_identity
    assert attempt["failure_fingerprints"] == expected_fingerprints
    assert attempt["source_before"] == source_before
    assert attempt["source_after_patch"] == project_source_fingerprint(project.root)
    assert attempt["repair_context"]["digest"]
    assert attempt["patch_digest"]
    assert attempt["diff_summary"][0]["characters"] > 0
    assert attempt["outcome"]["code"] == "PATCH_APPLIED"
    assert candidate["event"] == "candidate_ready"
    assert candidate["attempt_id"] == attempt["attempt_id"]
    assert candidate["tested_source"] == project_source_fingerprint(project.root)
    assert validation["event"] == "gate_result"
    assert validation["attempt_id"] == attempt["attempt_id"]
    assert validation["scope"] == "full"
    assert validation["tested_source"] == candidate["tested_source"]
    assert validation["decision"]["action"] == "PASS"
    assert validation["gate"]["passed"] is True

    artifact = json.loads(
        (project.workspace / "repair_001.json").read_text(encoding="utf-8")
    )
    assert artifact["attempt_id"] == attempt["attempt_id"]
    assert artifact["outcome"]["code"] == "PATCH_APPLIED"
    assert artifact["diffs"]


def test_gate_links_build_fixed_source_instead_of_prebuild_patch(project, monkeypatch):
    failure = report(project)
    ctx = wire(
        monkeypatch,
        project,
        [gate(failure), gate(passed=True)],
        rounds=2,
        build_mutations=["// build repair\n"],
    )

    result = run(ctx, project)

    assert result["success"]
    events = json.loads(
        (project.workspace / "repair_ledger.json").read_text(encoding="utf-8")
    )["events"]
    attempt, candidate, validation = events
    assert attempt["source_after_patch"] != candidate["tested_source"]
    assert validation["tested_source"] == candidate["tested_source"]
    assert validation["gate"]["passed"] is True


def test_repair_context_tracks_all_decision_inputs(tmp_path, monkeypatch):
    root = tmp_path / "project"
    workspace = tmp_path / "gate"
    root.mkdir()
    workspace.mkdir()
    manifest = root / "translation_manifest.json"
    plan_path = tmp_path / "translation_plan.json"
    manifest.write_text('{"mapping":"one"}', encoding="utf-8")
    plan_path.write_text('{"units":[]}', encoding="utf-8")
    (workspace / "page_pairs.json").write_text(
        '{"android.Main":"MainPage"}', encoding="utf-8"
    )
    (workspace / "unit_page_map.json").write_text(
        '{"main":["MainPage"]}', encoding="utf-8"
    )
    plan = {"units": []}

    baseline = functional.repair_context_fingerprint(
        root, plan_path, plan, "model-a", workspace=workspace
    )
    manifest.write_text('{"mapping":"two"}', encoding="utf-8")
    manifest_changed = functional.repair_context_fingerprint(
        root, plan_path, plan, "model-a", workspace=workspace
    )
    manifest.write_text('{"mapping":"one"}', encoding="utf-8")
    plan_path.write_text('{"units":[{"name":"changed"}]}', encoding="utf-8")
    plan_changed = functional.repair_context_fingerprint(
        root, plan_path, plan, "model-a", workspace=workspace
    )
    plan_path.write_text('{"units":[]}', encoding="utf-8")
    (workspace / "page_pairs.json").write_text(
        '{"android.Main":"OtherPage"}', encoding="utf-8"
    )
    page_pairs_changed = functional.repair_context_fingerprint(
        root, plan_path, plan, "model-a", workspace=workspace
    )
    (workspace / "page_pairs.json").write_text(
        '{"android.Main":"MainPage"}', encoding="utf-8"
    )
    (workspace / "unit_page_map.json").write_text(
        '{"main":["OtherPage"]}', encoding="utf-8"
    )
    unit_page_map_changed = functional.repair_context_fingerprint(
        root, plan_path, plan, "model-a", workspace=workspace
    )
    (workspace / "unit_page_map.json").write_text(
        '{"main":["MainPage"]}', encoding="utf-8"
    )
    monkeypatch.setattr(
        functional, "REPAIR_POLICY_VERSION", functional.REPAIR_POLICY_VERSION + 1
    )
    policy_changed = functional.repair_context_fingerprint(
        root, plan_path, plan, "model-a", workspace=workspace
    )
    monkeypatch.setattr(functional, "REPAIR_POLICY_VERSION", 1)
    monkeypatch.setattr(functional, "SYSTEM_PROMPT", functional.SYSTEM_PROMPT + "\nchanged")
    prompt_changed = functional.repair_context_fingerprint(
        root, plan_path, plan, "model-a", workspace=workspace
    )
    monkeypatch.undo()
    model_changed = functional.repair_context_fingerprint(
        root, plan_path, plan, "model-b", workspace=workspace
    )

    changed = {
        manifest_changed["digest"], plan_changed["digest"],
        page_pairs_changed["digest"], unit_page_map_changed["digest"],
        policy_changed["digest"], prompt_changed["digest"],
        model_changed["digest"],
    }
    assert baseline["digest"] not in changed
    assert len(changed) == 7


def test_same_source_and_failure_stops_before_second_model_call(project, monkeypatch):
    failure = report(project)
    first = wire(monkeypatch, project, [gate(failure)], rounds=2)
    _restore_real_report_fixer(first)
    first.loop.llm.invoke.return_value = "证据不足，与本文件无关"

    initial = run(first, project)

    assert initial["stop_reason"] == "MODEL_NO_PROPOSAL"
    first.loop.llm.invoke.assert_called_once()

    second = wire(monkeypatch, project, [gate(report(project))], rounds=2)
    _restore_real_report_fixer(second)

    repeated = run(second, project)

    assert repeated["stop_reason"] == "NO_PROGRESS"
    assert repeated["repair_iterations"] == 0
    second.loop.llm.invoke.assert_not_called()
    ledger = json.loads(
        (project.workspace / "repair_ledger.json").read_text(encoding="utf-8")
    )
    assert [event["event"] for event in ledger["events"]] == [
        "repair_attempt"
    ]


def test_a_to_b_to_a_cycle_stops_without_third_repair(project, monkeypatch):
    source_a = project.source.read_text(encoding="utf-8").rstrip()
    project.source.write_bytes(source_a.encode("utf-8"))
    source_b = source_a.replace("Text('old')", "Text('middle')")
    failure = report(project)
    ctx = wire(
        monkeypatch,
        project,
        [gate(failure), gate(failure), gate(failure)],
        rounds=3,
    )
    _restore_real_report_fixer(ctx)
    ctx.loop.llm.invoke.side_effect = [
        f"```typescript\n{source_b}\n```",
        f"```typescript\n{source_a}\n```",
    ]

    result = run(ctx, project)

    assert result["stop_reason"] == "NO_PROGRESS"
    assert result["repair_iterations"] == 2
    assert result["gate_calls"] == 3
    assert ctx.loop.llm.invoke.call_count == 2
    assert len(ctx.builds) == 2
    assert project.source.read_text(encoding="utf-8") == source_a
    assert project.source.with_suffix(".ets.funcbak").is_file()
    ledger = json.loads(
        (project.workspace / "repair_ledger.json").read_text(encoding="utf-8")
    )
    assert [event["event"] for event in ledger["events"]] == [
        "repair_attempt", "candidate_ready", "gate_result",
        "repair_attempt", "candidate_ready", "gate_result",
    ]
    assert (ledger["events"][0]["source_before"]
            == ledger["events"][3]["source_after_patch"])


@pytest.mark.parametrize(("case", "expected", "model_calls"), [
    ("unmapped", "UNMAPPED_REPORT", 0),
    ("no_proposal", "MODEL_NO_PROPOSAL", 1),
    ("no_op", "NO_OP_PATCH", 1),
    ("invalid", "INVALID_PATCH", 1),
])
def test_no_change_reasons_are_typed_and_persisted(
        project, monkeypatch, case, expected, model_calls):
    failure = report(project, located=case != "unmapped")
    ctx = wire(monkeypatch, project, [gate(failure)], rounds=2)
    _restore_real_report_fixer(ctx)
    responses = {
        "no_proposal": "无法提出代码方案",
        "no_op": (
            "```typescript\n"
            + project.source.read_text(encoding="utf-8")
            + "```"
        ),
        "invalid": "```typescript\nlet x = 1\n```",
    }
    if case in responses:
        ctx.loop.llm.invoke.return_value = responses[case]

    result = run(ctx, project)

    assert result["stop_reason"] == expected
    assert result["repair_outcome"]["code"] == expected
    assert result["repair_outcome"]["model_calls"] == model_calls
    assert result["repair_outcome"]["files_changed"] == 0
    assert ctx.loop.llm.invoke.call_count == model_calls
    artifact = json.loads(
        (project.workspace / "repair_001.json").read_text(encoding="utf-8")
    )
    assert artifact["outcome"] == result["repair_outcome"]
    ledger = json.loads(
        (project.workspace / "repair_ledger.json").read_text(encoding="utf-8")
    )
    assert ledger["events"][0]["outcome"] == result["repair_outcome"]


@pytest.mark.parametrize("contents", [
    "{",
    json.dumps({"schema_version": True, "events": []}),
    json.dumps({"schema_version": 2, "events": [{"event": "future_event"}]}),
    json.dumps({
        "schema_version": 2,
        "events": [{"event": "repair_attempt", "attempt_id": "incomplete"}],
    }),
])
def test_invalid_repair_ledger_fails_closed(project, monkeypatch, contents):
    project.workspace.mkdir(parents=True)
    (project.workspace / "repair_ledger.json").write_text(contents, encoding="utf-8")
    ctx = wire(monkeypatch, project, [gate(passed=True)], rounds=1)

    result = run(ctx, project)

    assert result["stop_reason"] == "REPAIR_LEDGER_INVALID"
    assert result["gate_calls"] == 0
    assert ctx.replays == []
    ctx.loop.llm.invoke.assert_not_called()


def test_cross_project_workspace_mismatch_does_not_overwrite_state(
        project, tmp_path, monkeypatch):
    first = wire(monkeypatch, project, [gate(passed=True)], rounds=1)
    assert run(first, project)["success"]
    state_before = (project.workspace / "functional_state.json").read_bytes()
    result_before = (project.workspace / "functional_result.json").read_bytes()

    other_root = tmp_path / "other-project"
    other_source = other_root / "entry/src/main/ets/pages/MainPage.ets"
    other_source.parent.mkdir(parents=True)
    other_source.write_text(
        "@Entry\n@Component\nstruct MainPage { build() { Text('other') } }\n",
        encoding="utf-8",
    )
    other = SimpleNamespace(
        root=other_root, source=other_source, workspace=project.workspace,
        sync=tmp_path / "other-sync", seeds=tmp_path / "other-seeds",
    )
    second = wire(monkeypatch, other, [gate(passed=True)], rounds=1)

    result = run(second, other)

    assert result["stop_reason"] == "REPAIR_LEDGER_INVALID"
    assert "PROJECT_MISMATCH" in result["detail"]
    assert second.replays == []
    assert (project.workspace / "functional_state.json").read_bytes() == state_before
    assert (project.workspace / "functional_result.json").read_bytes() == result_before


def test_partial_budget_gate_does_not_close_open_attempt(project, monkeypatch):
    source = project.source.read_text(encoding="utf-8")
    fixed = source.replace("Text('old')", "Text('fixed')")
    first = wire(
        monkeypatch, project, [gate(report(project))],
        builds=[False], rounds=2,
    )
    _restore_real_report_fixer(first)
    first.loop.llm.invoke.return_value = f"```typescript\n{fixed}\n```"
    assert run(first, project)["stop_reason"] == "BUILD_FAILED"

    error = BudgetExceeded("gate_budget")
    error.gate_result = gate(report(project), status="INCONCLUSIVE",
                             reason="BUDGET_EXHAUSTED")
    second = wire(monkeypatch, project, [error], current=False, rounds=2)

    with pytest.raises(BudgetExceeded):
        run(second, project)

    saved = json.loads(
        (project.workspace / "functional_result.json").read_text(encoding="utf-8")
    )
    assert saved["gate_calls"] == 1
    assert saved["full_gate_calls"] == 1
    assert saved["diagnostic_gate_calls"] == 0
    resumed = wire(monkeypatch, project, [gate(passed=True)], rounds=1)
    assert run(resumed, project)["success"]
    ledger = json.loads(
        (project.workspace / "repair_ledger.json").read_text(encoding="utf-8")
    )
    assert [event["event"] for event in ledger["events"]] == [
        "repair_attempt", "candidate_ready", "gate_result", "gate_result"
    ]
    assert ledger["events"][2]["decision"] is None
    assert ledger["events"][3]["decision"]["action"] == "PASS"

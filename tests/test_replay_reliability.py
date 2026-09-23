"""Behavioral gate contracts, without devices, network or model services."""
import json
from types import SimpleNamespace

import pytest

from diff_tester.config import Config
from diff_tester.gate import GateRequest, run_gate
from diff_tester.gate.repair_report import build_repair_report, cluster_reports
from diff_tester.metrics import compute_metrics
from diff_tester.oracle import PagePairs
from diff_tester.replayer import replay
from diff_tester.schemas import (AbstractEvent, ExternalSurfaceContract,
                                 StateVector, TargetFingerprint, Trace, UNode,
                                 save_json)
from diff_tester.state import UNCONFIRMED_EXTERNAL_SURFACE
from run_control import Budget, BudgetExceeded, budget_scope


def state(page="MainActivity", text="Welcome"):
    return StateVector(page, {text: 1}, {"button": [text]}, {}, {})


def seed(tid="seed"):
    before = state()
    event = AbstractEvent(1, "BACK", None, pre_state=before,
                          pre_state_hash=before.hash(), post_state=state("DetailActivity"))
    return Trace(tid, "android.app", "harmony.app", [event], initial_state=before)


class Device:
    def __init__(self, reset_error=None, execute_error=None,
                 action_errors=None, stable_results=None):
        self.reset_error, self.execute_error = reset_error, execute_error
        self.action_errors = dict(action_errors or {})
        self.stable_results = iter(stable_results) if stable_results is not None else None
        self.resets = self.executions = 0
        self.actions = []

    def reset_app(self):
        self.resets += 1
        if self.reset_error:
            raise self.reset_error

    def execute(self, event, *args):
        if event.action in self.action_errors:
            raise self.action_errors[event.action]
        if self.execute_error:
            raise self.execute_error
        self.executions += 1
        self.actions.append(event.action)

    def wait_stable(self):
        return next(self.stable_results) if self.stable_results is not None else True

    def dump_tree(self):
        return UNode("button", "open_picker", "Choose photo", "",
                     (0, 0, 100, 100), (0, 0, 0.2, 0.1), True, False, None)

    def ensure_ready(self):
        pass


def perform(monkeypatch, tmp_path, trace, device, states):
    iterator = iter(states)
    monkeypatch.setattr("diff_tester.replayer.alpha", lambda *a, **kw: next(iterator))
    return replay(trace, device, PagePairs({"MainActivity": "pages/Main", "DetailActivity": "pages/Detail"}),
                  Config(), str(tmp_path), collect_artifacts=False)


def external_protocol_seed(tid="seed"):
    before = state()
    target = TargetFingerprint(
        "button", "open_picker", "Choose photo", "",
        (0, 0, 0.2, 0.1),
    )
    event = AbstractEvent(
        1,
        "CLICK",
        target,
        pre_state=before,
        pre_state_hash=before.hash(),
        post_state=state(),
        external_surface=ExternalSurfaceContract("photo_picker"),
    )
    return Trace(tid, "android.app", "harmony.app", [event], initial_state=before)


def test_legacy_missing_initial_state_is_inconclusive_without_device(tmp_path, monkeypatch):
    trace = seed()
    trace.initial_state = None
    trace.schema_version = 1
    device = Device()
    result = perform(monkeypatch, tmp_path, trace, device, [])
    assert result.status == "INCONCLUSIVE" and result.stop_reason == "BASELINE_INVALID"
    assert result.executed_steps == result.verified_steps == device.resets == 0


def test_missing_post_baseline_never_passes(tmp_path, monkeypatch):
    trace = seed()
    trace.events[0].post_state = None
    result = perform(monkeypatch, tmp_path, trace, Device(), [])
    assert result.status == "INCONCLUSIVE" and not result.passed


def test_broken_baseline_state_chain_is_rejected_before_device_contact(tmp_path, monkeypatch):
    trace = seed()
    unexpected = state("UnexpectedActivity")
    trace.events.append(AbstractEvent(
        2,
        "BACK",
        None,
        pre_state=unexpected,
        pre_state_hash=unexpected.hash(),
        post_state=state("FinalActivity"),
    ))
    device = Device()

    result = perform(monkeypatch, tmp_path, trace, device, [])

    assert result.status == "INCONCLUSIVE" and result.stop_reason == "BASELINE_INVALID"
    assert result.divergence.cause_class == "baseline"
    assert any("step 1 post_state does not match step 2 pre_state"
               in error for error in result.divergence.detail["errors"])
    assert result.executed_steps == result.verified_steps == 0
    assert device.resets == device.executions == 0


def test_startup_wrong_page_clusters_across_traces_and_targets(tmp_path, monkeypatch):
    reports = []
    for index in range(8):
        trace, device = seed(f"seed{index}"), Device()
        result = perform(monkeypatch, tmp_path, trace, device, [state("pages/Wallpaper")])
        assert result.divergence.kind == "STARTUP_STATE_MISMATCH"
        assert result.executed_steps == 0 and device.executions == 0
        report = build_repair_report(trace, result, {"Home": {"android_pages": ["MainActivity"]}})
        assert set(report.suspect_units) == {"__global__", "Home"}
        reports.append(report)
    assert len(cluster_reports(reports)) == 1


def test_precondition_mismatch_uses_pre_page_not_destination(tmp_path, monkeypatch):
    trace, device = seed(), Device()
    result = perform(monkeypatch, tmp_path, trace, device,
                     [state("pages/Main"), state("pages/Wallpaper")])
    assert result.divergence.kind == "PRECONDITION_MISMATCH"
    report = build_repair_report(trace, result,
        {"Home": {"android_pages": ["MainActivity"]}, "Details": {"android_pages": ["DetailActivity"]}})
    assert report.expected["page"] == "MainActivity"
    assert report.suspect_units == ["Home"] and not report.action_executed


def test_cross_platform_states_compare_semantically_and_count_success(tmp_path, monkeypatch):
    trace, device = seed(), Device()
    result = perform(monkeypatch, tmp_path, trace, device,
                     [state("pages/Main"), state("pages/Main"), state("pages/Detail")])
    assert trace.events[0].pre_state_hash != state("pages/Main").hash()
    assert result.status == "PASS" and result.executed_steps == result.verified_steps == 1
    assert result.steps[0].verified and result.steps[0].action_executed
    metrics = compute_metrics([result])
    assert metrics["R_eq"] == 1 and metrics["per_trace"][0]["verified_steps"] == 1


def test_legacy_timestamp_drift_does_not_block_first_action(tmp_path, monkeypatch):
    trace, device = seed(), Device()
    trace.initial_state.texts = {"Welcome": 1, "上次更新时间：40 PM": 1}
    trace.events[0].pre_state.texts = dict(trace.initial_state.texts)
    trace.events[0].pre_state_hash = trace.events[0].pre_state.hash()
    actual = state("pages/Main")
    actual.texts = {"Welcome": 1, "上次更新时间：53 AM": 1}
    result = perform(monkeypatch, tmp_path, trace, device,
                     [actual, actual, state("pages/Detail")])
    assert result.status == "PASS"
    assert result.executed_steps == result.verified_steps == 1


def test_failed_comparison_is_executed_but_not_verified(tmp_path, monkeypatch):
    result = perform(monkeypatch, tmp_path, seed(), Device(),
                     [state("pages/Main"), state("pages/Main"), state("pages/Wrong")])
    assert result.status == "FAIL" and result.executed_steps == 1 and result.verified_steps == 0
    metrics = compute_metrics([result])
    assert metrics["compared_steps"] == 1 and metrics["verified_steps"] == 0


def test_declared_external_surface_is_recovered_then_app_state_is_verified(tmp_path, monkeypatch):
    trace = external_protocol_seed()
    # A system window may replace current_page while it is visible. The
    # recovered application state, rather than this transient page ID, is the
    # authoritative L1 comparison.
    external = state("system.photo_picker")
    external.external_surface = "photo_picker"
    device = Device()

    result = perform(monkeypatch, tmp_path, trace, device,
                     [state("pages/Main"), state("pages/Main"), external,
                      state("pages/Main")])

    assert result.status == "PASS"
    assert device.actions == ["CLICK", "BACK"]
    assert result.steps[0].passed and result.steps[0].verified
    assert result.steps[0].external_surface == {
        "id": "external_protocol",
        "schema_version": 1,
        "declared_surface": "photo_picker",
        "actual_surface": "photo_picker",
        "ownership": "system",
        "policy": "dismiss_and_compare",
        "accepted": True,
        "recovery_attempted": True,
        "recovered": True,
        "recovery_action": "BACK",
    }


def test_undeclared_external_surface_is_a_baseline_protocol_error(tmp_path, monkeypatch):
    external = state("pages/Detail")
    external.external_surface = "photo_picker"
    device = Device()

    result = perform(monkeypatch, tmp_path, seed(), device,
                     [state("pages/Main"), state("pages/Main"), external])

    assert result.status == "INCONCLUSIVE"
    assert result.divergence.kind == "EXTERNAL_PROTOCOL"
    assert result.divergence.cause_class == "baseline"
    assert not result.steps[0].verified and not result.steps[0].passed
    assert not result.steps[0].external_surface["accepted"]
    assert not result.steps[0].external_surface["recovery_attempted"]
    assert device.actions == ["BACK"]


def test_undeclared_unconfirmed_surface_records_unknown_ownership(tmp_path, monkeypatch):
    ambiguous = state("pages/Main")
    ambiguous.external_surface = UNCONFIRMED_EXTERNAL_SURFACE

    result = perform(
        monkeypatch,
        tmp_path,
        seed(),
        Device(),
        [state("pages/Main"), state("pages/Main"), ambiguous],
    )

    assert result.status == "INCONCLUSIVE"
    assert result.divergence.cause_class == "unknown"
    assert result.steps[0].external_surface["ownership"] == "unknown"
    assert result.divergence.detail["failed_predicates"] == [
        "external_protocol.ownership"
    ]


def test_declared_external_surface_must_appear(tmp_path, monkeypatch):
    device = Device()

    result = perform(monkeypatch, tmp_path, external_protocol_seed(), device,
                     [state("pages/Main"), state("pages/Main"), state("pages/Main")])

    assert result.status == "FAIL"
    assert result.divergence.kind == "EXTERNAL_PROTOCOL"
    assert result.divergence.cause_class == "translation"
    assert not result.steps[0].verified and not result.steps[0].passed
    assert result.steps[0].external_surface["actual_surface"] is None
    assert not result.steps[0].external_surface["recovery_attempted"]
    assert device.actions == ["CLICK"]


def test_missing_declared_surface_with_unknown_page_is_inconclusive(tmp_path, monkeypatch):
    device = Device()

    result = perform(
        monkeypatch,
        tmp_path,
        external_protocol_seed(),
        device,
        [state("pages/Main"), state("pages/Main"), state("system.unknown")],
    )

    assert result.status == "INCONCLUSIVE"
    assert result.divergence.kind == "EXTERNAL_PROTOCOL"
    assert result.divergence.cause_class == "unknown"
    observation = result.divergence.detail["ownership_observation"]
    assert observation == {
        "actual_page": "system.unknown",
        "application_page_confirmed": False,
    }
    assert device.actions == ["CLICK"]


def test_unconfirmed_external_ownership_never_routes_to_translation(tmp_path, monkeypatch):
    device = Device()
    ambiguous = state("pages/Main")
    ambiguous.external_surface = UNCONFIRMED_EXTERNAL_SURFACE

    result = perform(
        monkeypatch,
        tmp_path,
        external_protocol_seed(),
        device,
        [state("pages/Main"), state("pages/Main"), ambiguous],
    )

    assert result.status == "INCONCLUSIVE"
    assert result.divergence.kind == "EXTERNAL_PROTOCOL"
    assert result.divergence.cause_class == "unknown"
    assert result.steps[0].external_surface["actual_surface"] == "unconfirmed"
    assert not result.steps[0].external_surface["recovery_attempted"]
    assert device.actions == ["CLICK"]


def test_external_surface_type_mismatch_is_translation_failure(tmp_path, monkeypatch):
    external = state("pages/Main")
    external.external_surface = "document_picker"
    device = Device()

    result = perform(monkeypatch, tmp_path, external_protocol_seed(), device,
                     [state("pages/Main"), state("pages/Main"), external])

    assert result.status == "FAIL"
    assert result.divergence.kind == "EXTERNAL_PROTOCOL"
    assert result.divergence.cause_class == "translation"
    assert result.steps[0].external_surface["declared_surface"] == "photo_picker"
    assert result.steps[0].external_surface["actual_surface"] == "document_picker"
    assert not result.steps[0].external_surface["accepted"]
    assert device.actions == ["CLICK"]


def test_external_surface_that_remains_after_recovery_is_platform_failure(tmp_path, monkeypatch):
    trace = external_protocol_seed()
    external = state("pages/Main")
    external.external_surface = "photo_picker"
    device = Device()

    result = perform(monkeypatch, tmp_path, trace, device,
                     [state("pages/Main"), state("pages/Main"), external, external])

    assert result.status == "INCONCLUSIVE"
    assert result.divergence.kind == "EXTERNAL_PROTOCOL"
    assert result.divergence.cause_class == "platform"
    evidence = result.steps[0].external_surface
    assert not result.steps[0].verified and not result.steps[0].passed
    assert evidence["accepted"] and evidence["recovery_attempted"]
    assert not evidence["recovered"] and "remained" in evidence["recovery_error"]
    assert device.actions == ["CLICK", "BACK"]


def test_external_recovery_command_failure_is_platform_failure(tmp_path, monkeypatch):
    external = state("pages/Main")
    external.external_surface = "photo_picker"
    device = Device(action_errors={"BACK": RuntimeError("system back unavailable")})

    result = perform(monkeypatch, tmp_path, external_protocol_seed(), device,
                     [state("pages/Main"), state("pages/Main"), external])

    assert result.status == "INCONCLUSIVE"
    assert result.divergence.kind == "EXTERNAL_PROTOCOL"
    assert result.divergence.cause_class == "platform"
    evidence = result.steps[0].external_surface
    assert evidence["accepted"] and evidence["recovery_attempted"]
    assert not evidence["recovered"]
    assert evidence["recovery_error_type"] == "RuntimeError"
    assert device.actions == ["CLICK"]


def test_external_recovery_stability_failure_is_platform_failure(tmp_path, monkeypatch):
    external = state("pages/Main")
    external.external_surface = "photo_picker"
    device = Device(stable_results=[True, False])

    result = perform(monkeypatch, tmp_path, external_protocol_seed(), device,
                     [state("pages/Main"), state("pages/Main"), external])

    assert result.status == "INCONCLUSIVE"
    assert result.divergence.kind == "EXTERNAL_PROTOCOL"
    assert result.divergence.cause_class == "platform"
    evidence = result.steps[0].external_surface
    assert evidence["accepted"] and evidence["recovery_attempted"]
    assert not evidence["recovered"]
    assert "stabilize" in evidence["recovery_error"]
    assert device.actions == ["CLICK", "BACK"]


def test_external_action_settle_failure_keeps_unstable_diagnosis(tmp_path, monkeypatch):
    trace = external_protocol_seed()
    device = Device(stable_results=[False])

    result = perform(
        monkeypatch,
        tmp_path,
        trace,
        device,
        [state("pages/Main"), state("pages/Main")],
    )

    assert result.status == "INCONCLUSIVE"
    assert result.divergence.kind == "UNSTABLE"
    assert result.steps[0].phase == "settle"
    assert result.steps[0].external_surface is None
    assert result.contract_errors(trace) == []


def test_recovered_app_state_difference_remains_translation_failure(tmp_path, monkeypatch):
    external = state("pages/Main")
    external.external_surface = "photo_picker"

    result = perform(
        monkeypatch,
        tmp_path,
        external_protocol_seed(),
        Device(),
        [state("pages/Main"), state("pages/Main"), external,
         state("pages/Main", text="Different")],
    )

    assert result.status == "FAIL"
    assert result.divergence.kind == "L2_CONTENT"
    assert result.divergence.cause_class == "translation"
    assert result.steps[0].external_surface["recovered"] is True
    assert result.steps[0].verified and not result.steps[0].passed


@pytest.mark.parametrize("phase", ["startup", "precondition"])
def test_external_surface_before_action_is_baseline_protocol_error(
        tmp_path, monkeypatch, phase):
    external = state("system.photo_picker")
    external.external_surface = "photo_picker"
    states = [external] if phase == "startup" else [state("pages/Main"), external]
    device = Device()

    result = perform(monkeypatch, tmp_path, seed(), device, states)

    assert result.status == "INCONCLUSIVE"
    assert result.divergence.kind == "EXTERNAL_PROTOCOL"
    assert result.divergence.cause_class == "baseline"
    assert result.divergence.phase == phase
    assert not result.divergence.action_executed
    assert device.actions == []


@pytest.mark.parametrize("phase", ["reset", "execute"])
def test_device_error_is_infrastructure_not_translation(tmp_path, monkeypatch, phase):
    device = Device(**{f"{phase}_error": RuntimeError("device disconnected")})
    result = perform(monkeypatch, tmp_path, seed(), device, [state("pages/Main"), state("pages/Main")])
    assert result.status == "INCONCLUSIVE" and result.divergence.kind == "INFRA_ERROR"
    assert result.divergence.phase == phase and result.divergence.cause_class == "infrastructure"
    assert not result.divergence.action_executed


def test_budget_is_saved_and_propagated(tmp_path, monkeypatch):
    device = Device(execute_error=BudgetExceeded("test_limit"))
    with pytest.raises(BudgetExceeded) as error:
        perform(monkeypatch, tmp_path, seed(), device, [state("pages/Main"), state("pages/Main")])
    result = error.value.trace_result
    assert result.stop_reason == "BUDGET_EXHAUSTED"
    saved = json.loads((tmp_path / "seed/result.json").read_text(encoding="utf-8"))
    assert saved["status"] == "INCONCLUSIVE"


def test_startup_metrics_never_negative_or_false_hundred_percent(tmp_path, monkeypatch):
    result = perform(monkeypatch, tmp_path, seed(), Device(), [state("pages/Wrong")])
    metrics = compute_metrics([result])
    assert metrics["R_replay"] == 0 and metrics["R_eq"] is None
    assert metrics["avg_norm_divergence_depth"] == 0


def test_gate_persists_environment_failure_and_round_uses_max(tmp_path):
    save_json(seed().to_dict(), str(tmp_path / "seeds/seed.json"))
    save_json({}, str(tmp_path / "history/round_007.json"))
    device = Device()
    device.ensure_ready = lambda: (_ for _ in ()).throw(RuntimeError("offline"))
    result = run_gate(GateRequest("harmony.app", str(tmp_path)), harmony=device)
    assert result.status == "INCONCLUSIVE" and result.stop_reason == "INFRA_ERROR"
    assert result.round_no == 8 and result.consistency_rate is None
    assert (tmp_path / "history/round_008.json").is_file()


def test_command_timeout_uses_remaining_budget_and_rethrows(monkeypatch):
    from diff_tester.adapters.base import run_command
    timeouts = []
    monkeypatch.setattr("diff_tester.adapters.base.subprocess.run", lambda *a, **kw:
                        timeouts.append(kw["timeout"]) or SimpleNamespace(returncode=0, stdout="", stderr=""))
    with budget_scope(Budget(time_limit_s=1)):
        run_command(["fake"], timeout_s=30)
    assert 0 < timeouts[0] <= 1


def test_wait_stable_rejects_a_short_lived_identical_transition():
    from diff_tester.adapters.base import DeviceAdapter

    cfg = Config()
    cfg.device.stable_interval_s = 0.001
    cfg.device.stable_samples = 3
    frames = iter(["transition", "transition", "settled", "settled", "settled"])
    observed = []

    def dump_tree():
        text = next(frames)
        observed.append(text)
        return UNode("text", None, text, "", (0, 0, 1, 1),
                     (0, 0, 1, 1), False, False, None)

    adapter = SimpleNamespace(cfg=cfg, dump_tree=dump_tree)
    assert DeviceAdapter.wait_stable(adapter, timeout_s=1)
    assert observed == ["transition", "transition", "settled", "settled", "settled"]


def test_harmony_unsupported_rotation_cannot_silently_pass():
    from diff_tester.adapters.harmony import HarmonyAdapter
    from diff_tester.adapters.base import CommandError
    adapter = HarmonyAdapter(Config(), "harmony.app")
    with pytest.raises(CommandError, match="unsupported"):
        adapter.execute(AbstractEvent(1, "ROTATE", None), None)


def test_harmony_reset_failure_stops_before_launch(monkeypatch):
    from diff_tester.adapters.harmony import HarmonyAdapter
    from diff_tester.adapters.base import CommandError
    adapter = HarmonyAdapter(Config(), "harmony.app")
    calls = []
    def command(*args, **kwargs):
        calls.append(args)
        if "clean" in args:
            raise CommandError("data reset failed")
    monkeypatch.setattr(adapter, "_hdc", command)
    with pytest.raises(CommandError):
        adapter.reset_app()
    assert not any("start" in call for call in calls)


def test_normalized_tree_summary_prioritizes_relevant_leaf(tmp_path):
    from diff_tester.gate import RepairReport
    root = {"children": [{"children": [{"role": "button", "id": "more", "text": "More", "clickable": True}]}]}
    path = tmp_path / "tree.json"
    save_json(root, str(path))
    report = RepairReport("seed", "test", 1, "ALIGN_FAIL", {"target": {"id_hint": "more"}}, {}, {},
                          artifacts={"harmony_dump": str(path)})
    summary = json.loads(report._dump_summary())
    assert summary["nodes"][0]["id"] == "more"


def test_recorder_writes_v2_initial_and_event_pre_state(tmp_path):
    from diff_tester.explorer import explore
    tree = UNode("button", "next", "Welcome", "", (0, 0, 100, 100),
                 (0, 0, 1, 1), True, False, None)
    class Android(Device):
        pkg, serial = "android.app", "offline-fake"
        def dump_tree(self):
            return tree
        def current_page(self):
            return "MainActivity"
        def current_pkg(self):
            return self.pkg
        def app_alive(self):
            return True
        def poll_crash(self):
            return None
        def screenshot(self, path):
            raise RuntimeError("optional artifact unsupported")
    paths = explore(Android(), Config(), str(tmp_path), 1, 1)
    trace = Trace.from_dict(json.loads(open(paths[0], encoding="utf-8").read()))
    assert trace.schema_version == 2 and trace.initial_state.page == "MainActivity"
    assert trace.events[0].pre_state.page == "MainActivity"
    assert trace.baseline_errors() == []

    protocol_seed = external_protocol_seed("protocol-seed")
    protocol_seed.events[0].target = TargetFingerprint(
        "button", "next", "Welcome", "", (0, 0, 1, 1),
    )
    seeded_paths = explore(
        Android(), Config(), str(tmp_path / "seeded"), 1, 1,
        seeds=[protocol_seed],
    )
    refreshed = Trace.from_dict(
        json.loads(open(seeded_paths[0], encoding="utf-8").read())
    )
    assert refreshed.events[0].external_surface == ExternalSurfaceContract("photo_picker")
    assert refreshed.baseline_errors() == []


def test_seed_refresh_recovers_real_foreground_package_switch(tmp_path):
    from diff_tester.explorer import explore

    tree = UNode("button", "open_picker", "Choose photo", "",
                 (0, 0, 100, 100), (0, 0, 1, 1), True, False, None)

    class SwitchingAndroid(Device):
        pkg, serial = "android.app", "offline-fake"

        def __init__(self):
            super().__init__()
            self.foreground = self.pkg
            self.stability_checks = 0

        def reset_app(self):
            super().reset_app()
            self.foreground = self.pkg

        def dump_tree(self):
            return tree

        def current_page(self):
            return ("MainActivity" if self.foreground == self.pkg
                    else "PhotoPickerActivity")

        def current_pkg(self):
            return self.foreground

        def execute(self, event, *args):
            super().execute(event, *args)
            if event.action == "CLICK":
                self.foreground = "com.android.photopicker"
            elif event.action == "BACK":
                self.foreground = self.pkg

        def wait_stable(self):
            self.stability_checks += 1
            return True

        def app_alive(self):
            return True

        def poll_crash(self):
            return None

        def screenshot(self, path):
            raise RuntimeError("optional artifact unsupported")

    android = SwitchingAndroid()
    paths = explore(
        android,
        Config(),
        str(tmp_path),
        1,
        1,
        seeds=[external_protocol_seed("protocol-seed")],
    )
    refreshed = Trace.from_dict(
        json.loads(open(paths[0], encoding="utf-8").read())
    )

    assert android.actions == ["CLICK", "BACK"]
    assert android.stability_checks == 2
    assert android.current_pkg() == android.pkg
    assert refreshed.meta["seed_status"] == "completed"
    assert refreshed.meta["seed_prefix_expected_steps"] == 1
    assert refreshed.meta["seed_prefix_completed_steps"] == 1
    assert refreshed.events[0].post_state.page == "MainActivity"
    assert refreshed.events[0].post_state.external_surface is None
    assert refreshed.baseline_errors() == []


@pytest.mark.parametrize(("failure_mode", "expected_actions"), [
    ("ownership_unknown", ["CLICK"]),
    ("recovery_stuck", ["CLICK", "BACK"]),
])
def test_seed_refresh_failure_cannot_fall_back_to_random_trace(
        tmp_path, failure_mode, expected_actions):
    from diff_tester.explorer import explore

    tree = UNode("button", "open_picker", "Choose photo", "",
                 (0, 0, 100, 100), (0, 0, 1, 1), True, False, None)

    class FailingSeedAndroid(Device):
        pkg, serial = "android.app", "offline-fake"

        def __init__(self):
            super().__init__()
            self.foreground = self.pkg

        def reset_app(self):
            super().reset_app()
            self.foreground = self.pkg

        def dump_tree(self):
            return tree

        def current_page(self):
            return ("MainActivity" if self.foreground == self.pkg
                    else "PhotoPickerActivity")

        def current_pkg(self):
            return self.foreground

        def execute(self, event, *args):
            super().execute(event, *args)
            if event.action == "CLICK":
                self.foreground = (None if failure_mode == "ownership_unknown"
                                   else "com.android.photopicker")
            elif event.action == "BACK" and failure_mode != "recovery_stuck":
                self.foreground = self.pkg

        def wait_stable(self):
            return True

        def app_alive(self):
            return True

        def poll_crash(self):
            return None

        def screenshot(self, path):
            raise RuntimeError("optional artifact unsupported")

    android = FailingSeedAndroid()
    paths = explore(
        android,
        Config(),
        str(tmp_path),
        1,
        1,
        seeds=[external_protocol_seed("protocol-seed")],
    )
    failed = Trace.from_dict(
        json.loads(open(paths[0], encoding="utf-8").read())
    )

    assert len(paths) == 1
    assert android.resets == 1
    assert android.actions == expected_actions
    assert failed.events == []
    assert failed.meta["seed"] == "protocol-seed"
    assert failed.meta["seed_status"] == "failed"
    assert failed.meta["seed_prefix_expected_steps"] == 1
    assert failed.meta["seed_prefix_completed_steps"] == 0
    assert failed.meta["ended_by"] == "seed_prefix_failed"
    assert "trace has no events" in failed.baseline_errors()


def test_gate_budget_exception_keeps_partial_replay_metadata(tmp_path, monkeypatch):
    trace = seed()
    save_json(trace.to_dict(), str(tmp_path / "seeds/seed.json"))
    save_json({"MainActivity": "pages/Main", "DetailActivity": "pages/Detail"},
              str(tmp_path / "page_pairs.json"))
    states = iter([state("pages/Main"), state("pages/Main")])
    monkeypatch.setattr("diff_tester.replayer.alpha", lambda *a, **kw: next(states))
    with pytest.raises(BudgetExceeded) as error:
        run_gate(GateRequest("harmony.app", str(tmp_path)),
                 harmony=Device(execute_error=BudgetExceeded("fake_timeout")))
    assert error.value.gate_result.status == "INCONCLUSIVE"
    saved = json.loads((tmp_path / "history/round_001.json").read_text(encoding="utf-8"))
    assert saved["metrics"]["per_trace"][0]["status"] == "INCONCLUSIVE"
    assert saved["trace_status"]["seed"] == "INCONCLUSIVE"


def test_hdc_backend_executes_click_and_type_and_propagates_command_failure(monkeypatch):
    from diff_tester.adapters.harmony import HarmonyAdapter
    from diff_tester.adapters.base import CommandError
    adapter = HarmonyAdapter(Config(), "harmony.app")
    adapter.use_bounded_backend()
    node = UNode("textfield", "field", "", "", (0, 0, 100, 100),
                 (0, 0, 1, 1), True, True, None)
    calls = []
    monkeypatch.setattr(adapter, "_hdc", lambda *a, **kw: calls.append(a))
    monkeypatch.setattr("diff_tester.adapters.harmony.time.sleep", lambda *a: None)
    adapter.execute(AbstractEvent(1, "CLICK", None), node)
    adapter.execute(AbstractEvent(2, "TYPE", None, params={"text": "hello"}), node)
    assert calls[0] == ("shell", "uitest", "uiInput", "click", "50", "50")
    assert calls[-1] == ("shell", "uitest", "uiInput", "inputText", "50", "50", "hello")
    monkeypatch.setattr(adapter, "_hdc", lambda *a, **kw: (_ for _ in ()).throw(CommandError("failed")))
    with pytest.raises(CommandError):
        adapter.execute(AbstractEvent(3, "CLICK", None), node)

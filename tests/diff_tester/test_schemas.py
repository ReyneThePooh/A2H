"""数据结构序列化往返单测（轨迹 JSON 是三个子命令间的交换格式，必须无损）。"""
import json
from copy import deepcopy

import pytest

from diff_tester.schemas import (AbstractEvent, ExternalSurfaceContract,
                                 StateVector, TargetFingerprint, Trace,
                                 external_protocol_evidence_errors)


def _sample_trace():
    fp = TargetFingerprint(role="button", id_hint="btn_login", text="登录",
                           desc="", rel_bounds=(0.08, 0.86, 0.92, 0.93),
                           patch_path="artifacts/t/step1_target.png")
    sv = StateVector(
        page="MainActivity",
        texts={"欢迎, user01": 1, "<VOLATILE>": 2},
        widgets={"button": ["退出", "设置"]},
        values={"et_username": "user01", "cb_remember": "True"},
        list_counts={"rv_items": 5},
        screenshot="artifacts/t/step1.png",
        dump_path="artifacts/t/step1_dump.json",
    )
    ev1 = AbstractEvent(step=1, action="TYPE", target=fp,
                        params={"text": "user01"},
                        pre_state_hash="abc123", post_state=sv)
    ev2 = AbstractEvent(step=2, action="BACK", target=None)
    return Trace(trace_id="t013", app_pkg_android="com.example.app",
                 app_pkg_harmony="com.example.hm", events=[ev1, ev2],
                 meta={"recorded_at": "2026-09-04", "seed": None})


def test_trace_json_roundtrip():
    t = _sample_trace()
    d = json.loads(json.dumps(t.to_dict(), ensure_ascii=False))
    t2 = Trace.from_dict(d)

    assert t2.trace_id == "t013"
    assert len(t2.events) == 2

    e1 = t2.events[0]
    assert e1.action == "TYPE"
    assert e1.params == {"text": "user01"}
    assert e1.target.id_hint == "btn_login"
    assert e1.target.rel_bounds == (0.08, 0.86, 0.92, 0.93)
    assert e1.post_state.values["cb_remember"] == "True"
    assert e1.post_state.list_counts == {"rv_items": 5}
    # 状态哈希跨序列化稳定
    assert e1.post_state.hash() == t.events[0].post_state.hash()

    e2 = t2.events[1]
    assert e2.target is None and e2.post_state is None


@pytest.mark.parametrize("invalid_version", [True, 1.0, "1"])
def test_external_protocol_evidence_requires_integer_schema_version(invalid_version):
    evidence = {
        "id": "external_protocol",
        "schema_version": invalid_version,
        "declared_surface": "photo_picker",
        "actual_surface": "photo_picker",
        "ownership": "system",
        "policy": "dismiss_and_compare",
        "accepted": True,
        "recovery_attempted": True,
        "recovered": True,
        "recovery_action": "BACK",
    }

    assert "external protocol evidence schema version is unsupported" in (
        external_protocol_evidence_errors(evidence)
    )


def test_state_hash_reflects_semantic_fields_only():
    sv = _sample_trace().events[0].post_state
    h0 = sv.hash()
    sv.screenshot = "elsewhere.png"     # 附件路径不参与哈希
    sv.dump_path = "elsewhere.json"
    assert sv.hash() == h0
    sv.values["cb_remember"] = "False"  # 语义字段参与哈希
    assert sv.hash() != h0


def _protocol_state() -> StateVector:
    return StateVector(
        page="WallpaperActivity",
        texts={"选择背景": 1},
        widgets={"button": ["本地照片"]},
        values={"mode": "photo"},
        list_counts={"wallpapers": 3},
    )


def _protocol_target(**overrides) -> TargetFingerprint:
    values = {
        "role": "button",
        "id_hint": "pick_photo",
        "text": "本地照片",
        "desc": "",
        "rel_bounds": (0.1, 0.2, 0.5, 0.3),
    }
    values.update(overrides)
    return TargetFingerprint(**values)


def _protocol_trace(
    *,
    contract: ExternalSurfaceContract | None = None,
    action: str = "CLICK",
    target: TargetFingerprint | None = None,
    pre_state: StateVector | None = None,
    post_state: StateVector | None = None,
) -> Trace:
    before = pre_state or _protocol_state()
    event = AbstractEvent(
        step=1,
        action=action,
        target=_protocol_target() if target is None else target,
        pre_state=before,
        pre_state_hash=before.hash(),
        post_state=post_state or deepcopy(before),
        external_surface=contract or ExternalSurfaceContract("photo_picker"),
    )
    return Trace(
        trace_id="external_protocol",
        app_pkg_android="com.example.app",
        app_pkg_harmony="com.example.hm",
        events=[event],
        initial_state=deepcopy(before),
    )


def test_external_surface_contract_roundtrip_and_valid_baseline():
    trace = _protocol_trace()

    restored = Trace.from_dict(json.loads(json.dumps(trace.to_dict(), ensure_ascii=False)))

    assert restored.events[0].external_surface == ExternalSurfaceContract("photo_picker")
    assert restored.baseline_errors() == []


@pytest.mark.parametrize(("field", "value", "message"), [
    ("surface", "", "unsupported external surface"),
    ("surface", "unknown_picker", "unsupported external surface"),
    ("policy", "ignore", "unsupported external surface policy"),
    ("recovery_action", "HOME", "unsupported external recovery action"),
    ("ownership", "application", "unsupported external surface ownership"),
])
def test_external_surface_contract_rejects_unknown_vocabulary(field, value, message):
    values = ExternalSurfaceContract("photo_picker").to_dict()
    values[field] = value

    errors = _protocol_trace(contract=ExternalSurfaceContract(**values)).baseline_errors()

    assert any(message in error for error in errors)


def test_external_surface_contract_requires_identifiable_click_target():
    trace = _protocol_trace(action="WAIT_IDLE")
    trace.events[0].target = None

    errors = trace.baseline_errors()

    assert any("requires a targeted CLICK" in error for error in errors)

    trace.events[0].action = "CLICK"
    trace.events[0].target = _protocol_target(id_hint=None, text="", desc="")
    errors = trace.baseline_errors()
    assert any("requires a targeted CLICK" in error for error in errors)


@pytest.mark.parametrize(("field", "value"), [
    ("page", "OtherActivity"),
    ("texts", {"选择背景": 1, "已变化": 1}),
    ("widgets", {"button": ["相机"]}),
    ("values", {"mode": "camera"}),
    ("list_counts", {"wallpapers": 4}),
])
def test_dismiss_and_compare_requires_unchanged_source_app_semantics(field, value):
    before = _protocol_state()
    after = deepcopy(before)
    setattr(after, field, value)

    errors = _protocol_trace(pre_state=before, post_state=after).baseline_errors()

    assert any("requires unchanged source application semantics" in error
               for error in errors)


@pytest.mark.parametrize("state_name", ["initial_state", "pre_state", "post_state"])
def test_source_baseline_states_must_be_app_owned(state_name):
    trace = _protocol_trace()
    state = (trace.initial_state if state_name == "initial_state"
             else getattr(trace.events[0], state_name))
    state.external_surface = "photo_picker"

    errors = trace.baseline_errors()

    assert any(f"{state_name} must be app-owned" in error for error in errors)


def test_undeclared_external_surface_is_invalid_baseline_evidence():
    trace = _protocol_trace()
    trace.events[0].external_surface = None
    trace.events[0].post_state.external_surface = "photo_picker"

    errors = trace.baseline_errors()

    assert any("post_state must be app-owned" in error for error in errors)


def _state_chain_trace() -> Trace:
    initial = _protocol_state()
    middle = StateVector(
        page="EditorActivity",
        texts={"编辑背景": 1},
        widgets={"button": ["保存"]},
        values={"title": "demo"},
        list_counts={"layers": 2},
    )
    final = StateVector(
        page="PreviewActivity",
        texts={"预览": 1},
        widgets={"button": ["完成"]},
        values={"title": "demo"},
        list_counts={"layers": 2},
    )
    events = [
        AbstractEvent(
            step=1,
            action="BACK",
            target=None,
            pre_state=deepcopy(initial),
            pre_state_hash=initial.hash(),
            post_state=deepcopy(middle),
        ),
        AbstractEvent(
            step=2,
            action="BACK",
            target=None,
            pre_state=deepcopy(middle),
            pre_state_hash=middle.hash(),
            post_state=deepcopy(final),
        ),
    ]
    return Trace(
        trace_id="state_chain",
        app_pkg_android="com.example.app",
        app_pkg_harmony="com.example.hm",
        events=events,
        initial_state=deepcopy(initial),
    )


def test_baseline_state_chain_accepts_contiguous_semantics_with_distinct_artifacts():
    trace = _state_chain_trace()
    trace.initial_state.screenshot = "initial.png"
    trace.events[0].pre_state.screenshot = "step1_pre.png"
    trace.events[0].post_state.dump_path = "step1_post.json"
    trace.events[1].pre_state.dump_path = "step2_pre.json"

    assert trace.baseline_errors() == []


def test_baseline_state_chain_rejects_initial_state_discontinuity():
    trace = _state_chain_trace()
    trace.events[0].pre_state.texts = {"unexpected": 1}
    trace.events[0].pre_state_hash = trace.events[0].pre_state.hash()

    errors = trace.baseline_errors()

    assert "state chain discontinuity: initial_state does not match step 1 pre_state" in errors


def test_baseline_state_chain_rejects_inter_event_discontinuity():
    trace = _state_chain_trace()
    trace.events[1].pre_state.values = {"title": "other"}
    trace.events[1].pre_state_hash = trace.events[1].pre_state.hash()

    errors = trace.baseline_errors()

    assert "state chain discontinuity: step 1 post_state does not match step 2 pre_state" in errors


def test_legacy_divergence_without_cause_defaults_to_unknown():
    from diff_tester.schemas import DivergenceReport

    restored = DivergenceReport.from_dict({
        "trace_id": "legacy",
        "diverged_step": 1,
        "kind": "L2_CONTENT",
        "detail": {},
    })

    assert restored.cause_class == "unknown"

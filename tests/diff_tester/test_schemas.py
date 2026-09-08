"""数据结构序列化往返单测（轨迹 JSON 是三个子命令间的交换格式，必须无损）。"""
import json

from diff_tester.schemas import (AbstractEvent, StateVector, TargetFingerprint,
                                 Trace)


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


def test_state_hash_reflects_semantic_fields_only():
    sv = _sample_trace().events[0].post_state
    h0 = sv.hash()
    sv.screenshot = "elsewhere.png"     # 附件路径不参与哈希
    sv.dump_path = "elsewhere.json"
    assert sv.hash() == h0
    sv.values["cb_remember"] = "False"  # 语义字段参与哈希
    assert sv.hash() != h0

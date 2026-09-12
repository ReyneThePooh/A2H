"""gate 门禁子包离线单元测试（不依赖真机）。

覆盖：增量选择 / FLAKY 观察名单 / 修复报告构建与渲染 /
run_gate 端到端编排（注入 fake 适配器与 fake 回放函数）。
"""
import os

import pytest

from diff_tester.gate import (
    GLOBAL_UNIT,
    FlakyList,
    GateRequest,
    RepairReport,
    build_repair_report,
    run_gate,
    select_traces,
    trace_pages,
)
from diff_tester.gate.repair_report import (
    suspect_files_from_stack,
    suspect_units_for_page,
)
from diff_tester.schemas import (
    AbstractEvent,
    DivergenceReport,
    StateVector,
    StepRecord,
    TargetFingerprint,
    Trace,
    TraceResult,
    save_json,
)

UNIT_PAGE_MAP = {
    "Unit_Main": {
        "android_pages": ["MainActivity"],
        "harmony_pages": ["EntryAbility:pages/MainPage"],
    },
    "Unit_Profile": {
        "android_pages": ["ProfileActivity"],
        "harmony_pages": ["EntryAbility:pages/ProfilePage"],
    },
    GLOBAL_UNIT: {"android_pages": ["*"], "harmony_pages": ["*"]},
}

PAGE_PAIRS = {
    ".MainActivity": "EntryAbility:pages/MainPage",
    ".ProfileActivity": "EntryAbility:pages/ProfilePage",
}


# ---------------------------------------------------------------------------
# 构造工具
# ---------------------------------------------------------------------------

def make_state(page: str, texts=None) -> StateVector:
    return StateVector(
        page=page,
        texts=dict(texts or {"欢迎": 1}),
        widgets={"button": ["登录"]},
        values={},
        list_counts={},
    )


def make_trace(trace_id: str, pages: list[str]) -> Trace:
    events = []
    for i, page in enumerate(pages, start=1):
        events.append(AbstractEvent(
            step=i,
            action="CLICK",
            target=TargetFingerprint(
                role="button", id_hint=f"btn_{i}", text=f"按钮{i}",
                desc="", rel_bounds=(0.1, 0.1, 0.9, 0.2),
            ),
            post_state=make_state(page),
        ))
    return Trace(
        trace_id=trace_id,
        app_pkg_android="com.example.app",
        app_pkg_harmony="com.example.hm",
        events=events,
    )


def make_result(trace: Trace, passed: bool = True,
                diverged_step: int = 0, kind: str = "L2_CONTENT") -> TraceResult:
    result = TraceResult(
        trace_id=trace.trace_id,
        total_steps=len(trace.events),
        executed_steps=len(trace.events) if passed else diverged_step,
        passed=passed,
    )
    result.steps = [
        StepRecord(step=e.step, action=e.action,
                   expected_page=e.post_state.page if e.post_state else "")
        for e in trace.events
        if passed or e.step <= diverged_step
    ]
    if not passed:
        ev = trace.events[diverged_step - 1]
        result.first_divergence_step = diverged_step
        result.divergence = DivergenceReport(
            trace_id=trace.trace_id,
            diverged_step=diverged_step,
            kind=kind,
            detail={"missing_texts": ["昵称已更新"]},
            android_state=ev.post_state,
            harmony_state=make_state("EntryAbility:pages/OtherPage", {"错误": 1}),
        )
    return result


class FakeHarmony:
    """run_gate 只调用 install / ensure_ready（回放函数已注入 fake）。"""

    def __init__(self):
        self.installed: list[str] = []
        self.ready = False

    def install(self, hap_path: str):
        self.installed.append(hap_path)

    def ensure_ready(self):
        self.ready = True


class ReplaySequencer:
    """按轨迹 id 依次弹出预设 TraceResult（首跑、复跑确认、下一轮……）。"""

    def __init__(self, plan: dict[str, list[TraceResult]]):
        self.plan = {k: list(v) for k, v in plan.items()}
        self.calls: list[str] = []

    def __call__(self, trace, harmony, page_pairs, cfg, results_root):
        self.calls.append(trace.trace_id)
        seq = self.plan[trace.trace_id]
        return seq.pop(0) if len(seq) > 1 else seq[0]


def setup_workspace(tmp_path, traces: list[Trace]) -> str:
    workspace = str(tmp_path / "gate_ws")
    for t in traces:
        save_json(t.to_dict(), os.path.join(workspace, "seeds", f"{t.trace_id}.json"))
    save_json(UNIT_PAGE_MAP, os.path.join(workspace, "unit_page_map.json"))
    save_json(PAGE_PAIRS, os.path.join(workspace, "page_pairs.json"))
    return workspace


# ---------------------------------------------------------------------------
# selector
# ---------------------------------------------------------------------------

def test_trace_pages_dedup():
    t = make_trace("seed_01_主页", [".MainActivity", ".MainActivity", ".ProfileActivity"])
    assert trace_pages(t) == {".MainActivity", ".ProfileActivity"}


def test_select_incremental_by_unit():
    t_main = make_trace("seed_01_主页", [".MainActivity"])
    t_prof = make_trace("seed_02_资料页", [".MainActivity", ".ProfileActivity"])
    selected, skipped = select_traces(
        [t_main, t_prof], ["Unit_Profile"], UNIT_PAGE_MAP)
    assert [t.trace_id for t in selected] == ["seed_02_资料页"]
    assert [t.trace_id for t in skipped] == ["seed_01_主页"]


@pytest.mark.parametrize("changed_units", [
    [GLOBAL_UNIT],            # 全局改动
    ["Unit_Unknown"],         # 映射缺失
    [],                       # 空改动
])
def test_select_conservative_fallback(changed_units):
    traces = [make_trace("seed_01_主页", [".MainActivity"]),
              make_trace("seed_02_资料页", [".ProfileActivity"])]
    selected, skipped = select_traces(traces, changed_units, UNIT_PAGE_MAP)
    assert len(selected) == 2 and not skipped


def test_select_full_replay_flag():
    traces = [make_trace("seed_01_主页", [".MainActivity"])]
    selected, skipped = select_traces(traces, ["Unit_Profile"], UNIT_PAGE_MAP,
                                      full_replay=True)
    assert len(selected) == 1 and not skipped


# ---------------------------------------------------------------------------
# flaky
# ---------------------------------------------------------------------------

def test_flaky_mark_escalate_clear(tmp_path):
    path = str(tmp_path / "flaky.json")
    fl = FlakyList(path)
    assert fl.mark("t1") == 1
    assert not fl.escalated("t1")
    assert fl.mark("t1") == 2
    assert fl.escalated("t1")
    fl.clear("t1")
    assert not fl.escalated("t1")
    fl.mark("t2")
    fl.save()
    # 持久化跨轮次
    fl2 = FlakyList(path)
    assert fl2.counts == {"t2": 1}


# ---------------------------------------------------------------------------
# repair_report
# ---------------------------------------------------------------------------

def test_suspect_units_reverse_lookup():
    assert suspect_units_for_page(".MainActivity", UNIT_PAGE_MAP) == ["Unit_Main"]
    assert suspect_units_for_page("com.x.ProfileActivity", UNIT_PAGE_MAP) == ["Unit_Profile"]
    assert suspect_units_for_page("UnknownActivity", UNIT_PAGE_MAP) == []


def test_build_repair_report_fields_and_prompt():
    trace = make_trace("seed_03_登录后修改昵称",
                       [".MainActivity", ".ProfileActivity", ".ProfileActivity"])
    result = make_result(trace, passed=False, diverged_step=3, kind="L2_CONTENT")
    report = build_repair_report(trace, result, UNIT_PAGE_MAP)

    assert report.failure_type == "CONTENT_LOSS"
    assert report.diverged_step == 3
    assert report.trace_intent == "登录后修改昵称"
    assert report.suspect_units == ["Unit_Profile"]
    assert report.actual["missing_texts"] == ["昵称已更新"]
    assert len(report.prior_steps_summary) == 2
    assert report.signature() == ("seed_03_登录后修改昵称", 3, "CONTENT_LOSS")

    prompt = report.to_prompt()
    for fragment in ("功能一致性验证失败", "登录后修改昵称", "第 3 步",
                     "CONTENT_LOSS", "Unit_Profile", "昵称已更新"):
        assert fragment in prompt

    # 序列化往返
    restored = RepairReport.from_dict(report.to_dict())
    assert restored.signature() == report.signature()


def test_repair_report_kind_mapping():
    trace = make_trace("seed_01_主页", [".MainActivity"])
    for kind, expected_type in [("EXEC_UNMAPPED", "ALIGN_FAIL"),
                                ("L0_CRASH", "CRASH"),
                                ("L1_PAGE", "WRONG_PAGE")]:
        result = make_result(trace, passed=False, diverged_step=1, kind=kind)
        assert build_repair_report(trace, result).failure_type == expected_type


_SAMPLE_CRASH = """Generated by HiviewDFX@OpenHarmony
Device info:emulator
Reason:TypeError
Error name:TypeError
Error message:undefined is not callable
Stacktrace:
    at PUV2ViewBase (/system/stateMgmt.js:5592:1)
    at RainAdapter entry (entry/src/main/ets/pages/RainAdapter.ets:3:1)
    at aboutToAppear entry (entry/src/main/ets/pages/MainPage.ets:123:24)
    at aboutToAppear entry (entry/src/main/ets/pages/MainPage.ets:125:24)

HiLog:
09-10 21:32:50 I noise entry/src/main/ets/pages/Ignored.ets:1
"""


def test_suspect_files_from_stack(tmp_path):
    path = tmp_path / "crash_stack.txt"
    path.write_text(_SAMPLE_CRASH, encoding="utf-8")
    # 保持栈序、去重、不吃 HiLog 段里的引用
    assert suspect_files_from_stack(str(path)) == [
        "entry/src/main/ets/pages/RainAdapter.ets",
        "entry/src/main/ets/pages/MainPage.ets",
    ]
    assert suspect_files_from_stack(str(tmp_path / "missing.txt")) == []


def test_crash_report_prompt_step0_with_stack(tmp_path):
    path = tmp_path / "crash_stack.txt"
    path.write_text(_SAMPLE_CRASH, encoding="utf-8")
    report = RepairReport(
        trace_id="trace_000", trace_intent="更多天气预报", diverged_step=0,
        failure_type="CRASH", abstract_event={},
        expected={"page": "", "must_have_texts": []},
        actual={"page": "", "missing_texts": [],
                "crash_sig": "jscrash-com.example-1.log"},
        suspect_files=["entry/src/main/ets/pages/RainAdapter.ets",
                       "entry/src/main/ets/pages/MainPage.ets"],
        artifacts={"crash_stack": str(path)},
    )
    prompt = report.to_prompt()
    assert "冷启动阶段" in prompt
    assert "undefined is not callable" in prompt        # 栈摘要进入 prompt
    assert "Device info" not in prompt                  # 头部噪音被剪掉
    assert "Ignored.ets" not in prompt                  # HiLog 段被剪掉
    assert "MainPage.ets" in prompt
    # suspect_files 序列化往返
    restored = RepairReport.from_dict(report.to_dict())
    assert restored.suspect_files == report.suspect_files


# ---------------------------------------------------------------------------
# run_gate 端到端（fake 注入）
# ---------------------------------------------------------------------------

def test_run_gate_all_pass(tmp_path):
    t_main = make_trace("seed_01_主页", [".MainActivity"])
    t_prof = make_trace("seed_02_资料页", [".ProfileActivity"])
    workspace = setup_workspace(tmp_path, [t_main, t_prof])
    harmony = FakeHarmony()
    seq = ReplaySequencer({
        "seed_01_主页": [make_result(t_main)],
        "seed_02_资料页": [make_result(t_prof)],
    })

    result = run_gate(
        GateRequest(bundle="com.example.hm", workspace=workspace,
                    hap_path="out/entry-default.hap"),
        harmony=harmony, replay_fn=seq,
    )

    assert result.passed
    assert harmony.installed == ["out/entry-default.hap"] and harmony.ready
    assert result.ran_traces == ["seed_01_主页", "seed_02_资料页"]
    assert result.consistency_rate == 1.0
    assert os.path.exists(os.path.join(workspace, "history", "round_001.json"))


def test_run_gate_confirmed_divergence(tmp_path):
    t_main = make_trace("seed_01_主页", [".MainActivity"])
    t_prof = make_trace("seed_02_资料页", [".MainActivity", ".ProfileActivity"])
    workspace = setup_workspace(tmp_path, [t_main, t_prof])
    diverged = make_result(t_prof, passed=False, diverged_step=2)
    seq = ReplaySequencer({
        "seed_01_主页": [make_result(t_main)],
        "seed_02_资料页": [diverged, diverged],   # 首跑 + 复跑均分叉
    })

    # 增量：只改了 Unit_Profile → seed_01 被跳过
    result = run_gate(
        GateRequest(bundle="com.example.hm", workspace=workspace,
                    changed_units=["Unit_Profile"]),
        harmony=FakeHarmony(), replay_fn=seq,
    )

    assert not result.passed
    assert result.ran_traces == ["seed_02_资料页"]
    assert result.skipped_traces == ["seed_01_主页"]
    assert len(result.reports) == 1
    assert result.reports[0].suspect_units == ["Unit_Profile"]
    # 复跑确认应执行了两次回放
    assert seq.calls == ["seed_02_资料页", "seed_02_资料页"]


def test_run_gate_flaky_then_escalate(tmp_path):
    t_main = make_trace("seed_01_主页", [".MainActivity"])
    workspace = setup_workspace(tmp_path, [t_main])
    diverged = make_result(t_main, passed=False, diverged_step=1)
    ok = make_result(t_main)

    # 第 1 轮：首跑分叉、复跑通过 → FLAKY 放行
    r1 = run_gate(
        GateRequest(bundle="com.example.hm", workspace=workspace),
        harmony=FakeHarmony(),
        replay_fn=ReplaySequencer({"seed_01_主页": [diverged, ok]}),
    )
    assert r1.passed and r1.flaky == ["seed_01_主页"] and not r1.reports

    # 第 2 轮：再次 FLAKY → 连续两轮，升级为失败
    r2 = run_gate(
        GateRequest(bundle="com.example.hm", workspace=workspace),
        harmony=FakeHarmony(),
        replay_fn=ReplaySequencer({"seed_01_主页": [diverged, ok]}),
    )
    assert not r2.passed
    assert r2.round_no == 2
    assert len(r2.reports) == 1 and r2.reports[0].flaky_escalated


def test_run_gate_timeout(tmp_path):
    t_main = make_trace("seed_01_主页", [".MainActivity"])
    workspace = setup_workspace(tmp_path, [t_main])

    result = run_gate(
        GateRequest(bundle="com.example.hm", workspace=workspace,
                    time_budget_s=0.0),
        harmony=FakeHarmony(),
        replay_fn=ReplaySequencer({"seed_01_主页": [make_result(t_main)]}),
    )

    assert not result.passed
    assert result.reports[0].failure_type == "TIMEOUT"
    assert not result.ran_traces


def test_run_gate_missing_seeds(tmp_path):
    with pytest.raises(FileNotFoundError):
        run_gate(
            GateRequest(bundle="com.example.hm", workspace=str(tmp_path / "empty")),
            harmony=FakeHarmony(), replay_fn=lambda *a: None,
        )

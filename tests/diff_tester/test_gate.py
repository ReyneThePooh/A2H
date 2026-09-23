"""gate 门禁子包离线单元测试（不依赖真机）。

覆盖：增量选择 / FLAKY 观察名单 / 修复报告构建与渲染 /
run_gate 端到端编排（注入 fake 适配器与 fake 回放函数）。
"""
import os

import pytest
from run_control import BudgetExceeded

from diff_tester.gate import (
    GLOBAL_UNIT,
    FlakyList,
    GateRequest,
    GateResult,
    RepairReport,
    build_repair_report,
    filter_traces_by_max_steps,
    run_gate,
    select_shortest_cover,
    select_traces,
    trace_pages,
    trace_step_count,
)
from diff_tester.gate.repair_report import (
    suspect_files_from_stack,
    suspect_units_for_page,
)
from diff_tester.schemas import (
    AbstractEvent,
    DivergenceReport,
    ExternalSurfaceContract,
    StateVector,
    StepRecord,
    TargetFingerprint,
    Trace,
    TraceResult,
    load_json,
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
            pre_state=make_state(pages[max(0, i - 2)]),
        ))
    return Trace(
        trace_id=trace_id,
        app_pkg_android="com.example.app",
        app_pkg_harmony="com.example.hm",
        events=events,
        initial_state=make_state(pages[0]),
    )


def make_result(trace: Trace, passed: bool = True,
                diverged_step: int = 0, kind: str = "L2_CONTENT") -> TraceResult:
    result = TraceResult(
        trace_id=trace.trace_id,
        total_steps=len(trace.events),
        executed_steps=len(trace.events) if passed else diverged_step,
        passed=passed,
        verified_steps=len(trace.events) if passed else max(0, diverged_step - 1),
    )
    result.steps = [
        StepRecord(step=e.step, action=e.action,
                   expected_page=e.post_state.page if e.post_state else "",
                   match_kind="MATCHED" if e.target is not None else None,
                   verdict_kind=(kind if not passed and e.step == diverged_step
                                 else None),
                   phase="compare",
                   cause_class=("translation"
                                if not passed and e.step == diverged_step else ""),
                   verified=True, action_executed=True,
                   passed=passed or e.step < diverged_step,
                   status="PASS" if passed or e.step < diverged_step else "FAIL")
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
            cause_class="translation",
            action_executed=True,
        )
        result.stop_reason = kind
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


def test_trace_step_count_and_max_step_filter_preserve_order():
    short = make_trace("short", [".MainActivity"])
    long = make_trace("long", [".MainActivity", ".ProfileActivity"])

    assert trace_step_count(short) == 1
    selected, skipped = filter_traces_by_max_steps([long, short], max_steps=1)
    assert [trace.trace_id for trace in selected] == ["short"]
    assert [trace.trace_id for trace in skipped] == ["long"]
    # No option is a no-op, including ordering.
    selected, skipped = filter_traces_by_max_steps([long, short])
    assert [trace.trace_id for trace in selected] == ["long", "short"]
    assert skipped == []


def test_select_shortest_cover_prefers_shorter_trace_for_equal_page_gain():
    long_main = make_trace("long-main", [".MainActivity"] * 3)
    short_main = make_trace("short-main", [".MainActivity"])
    profile = make_trace("profile", [".ProfileActivity", ".ProfileActivity"])

    selected, skipped = select_shortest_cover(
        [long_main, short_main, profile], max_traces=2)

    assert [trace.trace_id for trace in selected] == ["short-main", "profile"]
    assert [trace.trace_id for trace in skipped] == ["long-main"]


def test_select_shortest_cover_applies_max_steps_before_coverage():
    long_main = make_trace("long-main", [".MainActivity"] * 3)
    short_profile = make_trace("short-profile", [".ProfileActivity"])

    selected, skipped = select_shortest_cover(
        [long_main, short_profile], max_steps=1)

    assert [trace.trace_id for trace in selected] == ["short-profile"]
    assert [trace.trace_id for trace in skipped] == ["long-main"]


def test_incremental_selection_max_steps_is_opt_in():
    short = make_trace("short", [".ProfileActivity"])
    long = make_trace("long", [".ProfileActivity"] * 2)

    selected, skipped = select_traces(
        [short, long], ["Unit_Profile"], UNIT_PAGE_MAP, max_steps=1)
    assert [trace.trace_id for trace in selected] == ["short"]
    assert [trace.trace_id for trace in skipped] == ["long"]

    # The legacy call still selects both matching traces.
    selected, skipped = select_traces(
        [short, long], ["Unit_Profile"], UNIT_PAGE_MAP)
    assert [trace.trace_id for trace in selected] == ["short", "long"]
    assert skipped == []


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

    result.divergence.detail["failed_predicates"] = ["l2.text_multiset"]
    report = build_repair_report(trace, result, UNIT_PAGE_MAP)
    assert report.evidence["detail"]["failed_predicates"] == ["l2.text_multiset"]

    prompt = report.to_prompt()
    for fragment in ("功能一致性验证失败", "登录后修改昵称", "第 3 步",
                     "CONTENT_LOSS", "Unit_Profile", "昵称已更新",
                     "l2.text_multiset"):
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
    hap_path = tmp_path / "entry-default.hap"
    hap_path.write_bytes(b"fake-hap")
    seq = ReplaySequencer({
        "seed_01_主页": [make_result(t_main)],
        "seed_02_资料页": [make_result(t_prof)],
    })

    result = run_gate(
        GateRequest(bundle="com.example.hm", workspace=workspace,
                    hap_path=str(hap_path)),
        harmony=harmony, replay_fn=seq,
    )

    assert result.passed
    assert harmony.installed == [str(hap_path)] and harmony.ready
    assert result.ran_traces == ["seed_01_主页", "seed_02_资料页"]
    assert result.consistency_rate == 1.0
    assert os.path.exists(os.path.join(workspace, "history", "round_001.json"))


@pytest.mark.parametrize("invalid_step_evidence", ["failed", "missing"])
def test_gate_rejects_pass_summary_with_invalid_step_evidence(
        tmp_path, invalid_step_evidence):
    trace = make_trace("invalid-pass", [".MainActivity"])
    workspace = setup_workspace(tmp_path, [trace])
    invalid = make_result(trace)
    if invalid_step_evidence == "failed":
        invalid.steps[0].passed = False
        invalid.steps[0].status = "FAIL"
    else:
        invalid.steps.clear()
    seq = ReplaySequencer({trace.trace_id: [invalid]})

    result = run_gate(
        GateRequest(bundle="com.example.hm", workspace=workspace),
        harmony=FakeHarmony(), replay_fn=seq,
    )

    assert result.status == "INCONCLUSIVE"
    assert result.stop_reason == "INFRA_ERROR"
    assert result.trace_status[trace.trace_id] == "INCONCLUSIVE"
    assert result.flaky == []
    assert seq.calls == [trace.trace_id]
    assert len(result.reports) == 1
    report = result.reports[0]
    assert report.failure_type == "INFRA_ERROR"
    assert report.cause_class == "infrastructure"
    assert report.evidence["confirmed"] is None
    assert report.evidence["detail"]["attempt"] == "replay"
    assert report.evidence["detail"]["contract_errors"]


def _assert_failure_contract_quarantine(
        tmp_path, trace, invalid, expected_error: str):
    contract_errors = invalid.contract_errors(trace)
    assert any(expected_error in error for error in contract_errors)

    workspace = setup_workspace(tmp_path, [trace])
    seq = ReplaySequencer({trace.trace_id: [invalid]})
    result = run_gate(
        GateRequest(bundle="com.example.hm", workspace=workspace),
        harmony=FakeHarmony(), replay_fn=seq,
    )

    assert result.status == "INCONCLUSIVE"
    assert result.stop_reason == "INFRA_ERROR"
    assert result.trace_status[trace.trace_id] == "INCONCLUSIVE"
    assert result.flaky == []
    assert seq.calls == [trace.trace_id]
    assert len(result.reports) == 1
    report = result.reports[0]
    assert report.failure_type == "INFRA_ERROR"
    assert report.cause_class == "infrastructure"
    assert report.evidence["confirmed"] is None
    gate_errors = report.evidence["detail"]["contract_errors"]
    assert any(expected_error in error for error in gate_errors)


def test_gate_rejects_fail_summary_when_every_step_passed(tmp_path):
    trace = make_trace("fail-with-passing-step", [".MainActivity"])
    invalid = make_result(trace, passed=False, diverged_step=1)
    terminal = invalid.steps[-1]
    terminal.passed = True
    terminal.status = "PASS"
    terminal.verdict_kind = None
    invalid.verified_steps = 1

    _assert_failure_contract_quarantine(
        tmp_path, trace, invalid, "divergence terminal step is marked passed")


def test_gate_rejects_failure_without_divergence_step_record(tmp_path):
    trace = make_trace("failure-without-terminal", [".MainActivity"])
    invalid = make_result(trace, passed=False, diverged_step=1)
    invalid.steps.clear()
    invalid.executed_steps = 0
    invalid.verified_steps = 0

    _assert_failure_contract_quarantine(
        tmp_path, trace, invalid, "divergence has no matching terminal step record")


def test_gate_rejects_step_inconclusive_laundered_as_translation_failure(tmp_path):
    trace = make_trace("laundered-inconclusive", [".MainActivity"])
    invalid = make_result(trace, passed=False, diverged_step=1)
    terminal = invalid.steps[-1]
    terminal.status = "INCONCLUSIVE"
    terminal.verdict_kind = "INFRA_ERROR"
    terminal.phase = "execute"
    terminal.cause_class = "infrastructure"
    terminal.action_executed = False
    terminal.verified = False
    invalid.executed_steps = 0

    _assert_failure_contract_quarantine(
        tmp_path, trace, invalid, "terminal step status disagrees with result status")


def test_gate_rejects_divergence_state_from_unrelated_baseline(tmp_path):
    trace = make_trace("forged-divergence-state", [".MainActivity"])
    invalid = make_result(trace, passed=False, diverged_step=1)
    invalid.divergence.android_state = make_state(
        ".UnrelatedActivity", {"伪造状态": 1})

    _assert_failure_contract_quarantine(
        tmp_path, trace, invalid,
        "divergence Android state disagrees with the trace baseline")


def test_invalid_confirmation_result_stops_without_flaky_or_confirmation(tmp_path):
    trace = make_trace("invalid-confirmation", [".MainActivity"])
    workspace = setup_workspace(tmp_path, [trace])
    initial = make_result(trace, passed=False, diverged_step=1)
    invalid_confirmation = make_result(trace)
    invalid_confirmation.steps.clear()
    seq = ReplaySequencer({trace.trace_id: [initial, invalid_confirmation]})

    result = run_gate(
        GateRequest(bundle="com.example.hm", workspace=workspace),
        harmony=FakeHarmony(), replay_fn=seq,
    )

    assert result.status == "INCONCLUSIVE"
    assert result.stop_reason == "INFRA_ERROR"
    assert result.trace_status[trace.trace_id] == "INCONCLUSIVE"
    assert result.flaky == []
    assert seq.calls == [trace.trace_id, trace.trace_id]
    assert len(result.reports) == 1
    report = result.reports[0]
    assert report.failure_type == "INFRA_ERROR"
    assert report.cause_class == "infrastructure"
    assert report.evidence["confirmed"] is None
    assert report.evidence["detail"]["attempt"] == "confirmation"
    assert report.evidence["detail"]["contract_errors"]
    assert result.metrics["per_trace"][0]["kind"] == "INFRA_ERROR"


def test_declared_external_protocol_cannot_pass_without_step_evidence(tmp_path):
    trace = make_trace("missing-external-evidence", [".MainActivity"])
    trace.events[0].external_surface = ExternalSurfaceContract("photo_picker")
    workspace = setup_workspace(tmp_path, [trace])
    invalid = make_result(trace)

    result = run_gate(
        GateRequest(bundle="com.example.hm", workspace=workspace),
        harmony=FakeHarmony(),
        replay_fn=ReplaySequencer({trace.trace_id: [invalid]}),
    )

    assert result.status == "INCONCLUSIVE"
    assert result.stop_reason == "INFRA_ERROR"
    assert result.metrics["direct_verified_steps"] == 0
    errors = result.reports[0].evidence["detail"]["contract_errors"]
    assert any("declared external protocol has no evidence" in error
               for error in errors)


def test_launch_record_cannot_replace_a_planned_step(tmp_path):
    trace = make_trace("launch-substitution", [".MainActivity"])
    workspace = setup_workspace(tmp_path, [trace])
    invalid = make_result(trace)
    invalid.steps = [
        StepRecord(
            step=0, action="launch", passed=True, verified=True,
            action_executed=True, status="PASS",
        ),
        StepRecord(step=1, action="CLICK", status="NOT_RUN"),
    ]

    result = run_gate(
        GateRequest(bundle="com.example.hm", workspace=workspace),
        harmony=FakeHarmony(),
        replay_fn=ReplaySequencer({trace.trace_id: [invalid]}),
    )

    assert result.status == "INCONCLUSIVE"
    assert result.stop_reason == "INFRA_ERROR"
    assert result.metrics["direct_verified_steps"] == 0
    assert result.metrics["trace_pass_rate"] == 0.0


@pytest.mark.parametrize(("field", "value"), [
    ("match_kind", "UNMAPPED"),
    ("verdict_kind", "L2_CONTENT"),
    ("unstable", True),
])
def test_pass_step_cannot_carry_explicit_failure_evidence(
        tmp_path, field, value):
    trace = make_trace(f"contradictory-{field}", [".MainActivity"])
    workspace = setup_workspace(tmp_path, [trace])
    invalid = make_result(trace)
    setattr(invalid.steps[0], field, value)

    result = run_gate(
        GateRequest(bundle="com.example.hm", workspace=workspace),
        harmony=FakeHarmony(),
        replay_fn=ReplaySequencer({trace.trace_id: [invalid]}),
    )

    assert result.status == "INCONCLUSIVE"
    assert result.stop_reason == "INFRA_ERROR"
    assert result.metrics["trace_pass_rate"] == 0.0


def test_targetless_pass_cannot_carry_match_evidence(tmp_path):
    trace = make_trace("targetless-match", [".MainActivity"])
    trace.events[0].target = None
    workspace = setup_workspace(tmp_path, [trace])
    invalid = make_result(trace)
    invalid.steps[0].match_kind = "UNMAPPED"

    result = run_gate(
        GateRequest(bundle="com.example.hm", workspace=workspace),
        harmony=FakeHarmony(),
        replay_fn=ReplaySequencer({trace.trace_id: [invalid]}),
    )

    assert result.status == "INCONCLUSIVE"
    assert result.stop_reason == "INFRA_ERROR"
    assert result.metrics["trace_pass_rate"] == 0.0


def test_inconclusive_confirmation_preserves_infrastructure_failure(tmp_path):
    trace = make_trace("inconclusive-confirmation", [".MainActivity"])
    workspace = setup_workspace(tmp_path, [trace])
    initial = make_result(trace, passed=False, diverged_step=1)
    confirmation = TraceResult(
        trace_id=trace.trace_id,
        total_steps=1,
        executed_steps=0,
        verified_steps=0,
        passed=False,
        status="INCONCLUSIVE",
        stop_reason="INFRA_ERROR",
        first_divergence_step=0,
        divergence=DivergenceReport(
            trace_id=trace.trace_id,
            diverged_step=0,
            kind="INFRA_ERROR",
            detail={"error": "device disconnected"},
            android_state=trace.initial_state,
            harmony_state=None,
            phase="confirmation",
            cause_class="infrastructure",
        ),
        steps=[StepRecord(
            step=0,
            action="launch",
            verdict_kind="INFRA_ERROR",
            phase="confirmation",
            cause_class="infrastructure",
            status="INCONCLUSIVE",
        )],
    )
    assert confirmation.contract_errors(trace) == []

    result = run_gate(
        GateRequest(bundle="com.example.hm", workspace=workspace),
        harmony=FakeHarmony(),
        replay_fn=ReplaySequencer({trace.trace_id: [initial, confirmation]}),
    )

    assert result.status == "INCONCLUSIVE"
    assert result.stop_reason == "INFRA_ERROR"
    assert result.flaky == []
    assert result.reports[0].failure_type == "INFRA_ERROR"
    assert result.reports[0].cause_class == "infrastructure"
    assert result.reports[0].evidence["detail"]["error"] == "device disconnected"


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
    assert result.reports[0].suspect_units == ["Unit_Main", "Unit_Profile"]
    # 复跑确认应执行了两次回放
    assert seq.calls == ["seed_02_资料页", "seed_02_资料页"]
    assert result.reports[0].evidence["confirmed"] is True


def test_external_protocol_confirmation_requires_same_observed_surface(tmp_path):
    trace = make_trace("picker", [".MainActivity"])
    workspace = setup_workspace(tmp_path, [trace])
    first = make_result(trace, passed=False, diverged_step=1,
                        kind="EXTERNAL_PROTOCOL")
    confirm = make_result(trace, passed=False, diverged_step=1,
                          kind="EXTERNAL_PROTOCOL")
    first.divergence.detail = {
        "failed_predicates": ["external_protocol.surface"],
        "protocol_error": "declared external surface did not appear",
        "external_protocol": {
            "declared_surface": "photo_picker", "actual_surface": None,
            "accepted": False, "recovery_attempted": False,
        },
    }
    confirm.divergence.detail = {
        "failed_predicates": ["external_protocol.surface"],
        "protocol_error": "detected external surface does not match the contract",
        "external_protocol": {
            "declared_surface": "photo_picker", "actual_surface": "document_picker",
            "accepted": False, "recovery_attempted": False,
        },
    }

    result = run_gate(
        GateRequest(bundle="com.example.hm", workspace=workspace),
        harmony=FakeHarmony(),
        replay_fn=ReplaySequencer({"picker": [first, confirm]}),
    )

    assert result.status == "INCONCLUSIVE"
    assert result.flaky == ["picker"]
    assert result.reports[0].evidence["confirmed"] is None


def test_confirmation_uses_full_l2_evidence_not_coarse_cluster_key(tmp_path):
    trace = make_trace("values", [".MainActivity"])
    workspace = setup_workspace(tmp_path, [trace])
    first = make_result(trace, passed=False, diverged_step=1)
    confirm = make_result(trace, passed=False, diverged_step=1)
    first.divergence.detail = {
        "failed_predicates": ["l2.values"],
        "value_mismatches": {"mode": {"expected": "light", "actual": "dark"}},
    }
    confirm.divergence.detail = {
        "failed_predicates": ["l2.values"],
        "value_mismatches": {"mode": {"expected": "light", "actual": "auto"}},
    }
    first_report = build_repair_report(trace, first, {})
    confirm_report = build_repair_report(trace, confirm, {})
    assert first_report.root_cause_key == confirm_report.root_cause_key
    assert (first_report.confirmation_fingerprint
            != confirm_report.confirmation_fingerprint)

    result = run_gate(
        GateRequest(bundle="com.example.hm", workspace=workspace),
        harmony=FakeHarmony(),
        replay_fn=ReplaySequencer({"values": [first, confirm]}),
    )

    assert result.status == "INCONCLUSIVE"
    assert result.flaky == ["values"]
    assert result.reports[0].failure_type == "FLAKY"


def test_repair_gate_replays_failures_once_without_claiming_confirmation(tmp_path, capsys):
    traces = [make_trace("first", [".MainActivity"]),
              make_trace("second", [".MainActivity"])]
    workspace = setup_workspace(tmp_path, traces)
    seq = ReplaySequencer({trace.trace_id: [make_result(trace, False, 1)]
                           for trace in traces})
    result = run_gate(GateRequest("com.example.hm", workspace, confirm_failures=False),
                      harmony=FakeHarmony(), replay_fn=seq)
    assert seq.calls == ["first", "second"]
    assert result.status == "FAIL" and not result.passed
    assert len(result.reports) == 2
    assert all(report.evidence["confirmed"] is None for report in result.reports)
    assert result.metrics["replay_attempts"] == 2
    assert result.metrics["confirm_failures"] is False
    assert not os.path.exists(os.path.join(workspace, "runs", "round_001", "confirm"))
    output = capsys.readouterr().out
    assert "1/2 first: replay" in output and "second: FAIL" in output


def test_explicit_trace_ids_run_only_requested_diagnostics(tmp_path):
    traces = [make_trace("first", [".MainActivity"]),
              make_trace("second", [".MainActivity"])]
    workspace = setup_workspace(tmp_path, traces)
    seq = ReplaySequencer({"second": [make_result(traces[1])]})

    result = run_gate(
        GateRequest("com.example.hm", workspace, full_replay=True,
                    confirm_failures=False, trace_ids=["second"]),
        harmony=FakeHarmony(), replay_fn=seq,
    )

    assert result.passed
    assert result.ran_traces == ["second"]
    assert result.skipped_traces == ["first"]
    assert seq.calls == ["second"]


def test_gate_preserves_per_trace_mediation_evidence_and_only_appends_not_run(tmp_path):
    traces = [make_trace("selected", [".MainActivity"]),
              make_trace("skipped", [".MainActivity"])]
    traces[0].events[0].external_surface = ExternalSurfaceContract("photo_picker")
    workspace = setup_workspace(tmp_path, traces)
    replayed = make_result(traces[0])
    replayed.steps[0].external_surface = {
        "id": "external_protocol", "schema_version": 1,
        "declared_surface": "photo_picker", "actual_surface": "photo_picker",
        "ownership": "system", "policy": "dismiss_and_compare",
        "accepted": True, "recovery_attempted": True, "recovered": True,
        "recovery_action": "BACK",
    }

    result = run_gate(
        GateRequest("com.example.hm", workspace, full_replay=True,
                    confirm_failures=False, trace_ids=["selected"]),
        harmony=FakeHarmony(),
        replay_fn=ReplaySequencer({"selected": [replayed]}),
    )

    rows = {row["trace_id"]: row for row in result.metrics["per_trace"]}
    assert rows["selected"]["direct_verified_steps"] == 0
    assert rows["selected"]["mediated_steps"] == 1
    assert rows["selected"]["R_eq_direct"] == 0.0
    assert rows["selected"]["R_eq_policy"] == 1.0
    assert rows["selected"]["kind"] is None
    assert rows["skipped"]["status"] == "NOT_RUN"
    assert rows["skipped"]["mediated_steps"] == 0
    assert rows["skipped"]["R_eq_policy"] is None


def test_unknown_explicit_trace_id_is_inconclusive(tmp_path):
    trace = make_trace("known", [".MainActivity"])
    workspace = setup_workspace(tmp_path, [trace])

    result = run_gate(
        GateRequest("com.example.hm", workspace, trace_ids=["missing"]),
        harmony=FakeHarmony(), replay_fn=lambda *args: make_result(trace),
    )

    assert result.status == "INCONCLUSIVE"
    assert result.stop_reason == "BASELINE_INVALID"


def test_interrupted_round_artifacts_are_not_reused(tmp_path):
    trace = make_trace("seed", [".MainActivity"])
    workspace = setup_workspace(tmp_path, [trace])
    interrupted = tmp_path / "gate_ws/runs/round_003/seed/result.json"
    interrupted.parent.mkdir(parents=True)
    interrupted.write_text('{"incomplete": true}', encoding="utf-8")
    result = run_gate(GateRequest("com.example.hm", workspace), harmony=FakeHarmony(),
                      replay_fn=ReplaySequencer({"seed": [make_result(trace)]}))
    assert result.round_no == 4
    assert interrupted.read_text(encoding="utf-8") == '{"incomplete": true}'


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
    assert not r1.passed and r1.status == "INCONCLUSIVE"
    assert r1.flaky == ["seed_01_主页"] and r1.reports[0].failure_type == "FLAKY"
    assert r1.consistency_rate is None
    persisted = TraceResult.from_dict(load_json(os.path.join(
        workspace, "runs", "round_001", t_main.trace_id, "result.json")))
    assert persisted.status == "INCONCLUSIVE"
    assert persisted.stop_reason == "FLAKY"
    assert persisted.steps[-1].status == "INCONCLUSIVE"
    assert persisted.contract_errors(t_main) == []

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

    with pytest.raises(BudgetExceeded) as error:
        run_gate(
            GateRequest(bundle="com.example.hm", workspace=workspace,
                        time_budget_s=0.0),
            harmony=FakeHarmony(),
            replay_fn=ReplaySequencer({"seed_01_主页": [make_result(t_main)]}),
        )
    result = error.value.gate_result

    assert not result.passed
    assert result.reports[0].failure_type == "BUDGET_EXHAUSTED"
    assert not result.ran_traces


def test_gate_quarantines_and_persists_invalid_budget_partial(tmp_path):
    trace = make_trace("invalid-budget-partial", [".MainActivity"])
    workspace = setup_workspace(tmp_path, [trace])
    invalid = make_result(trace, passed=False, diverged_step=1)
    invalid.steps[-1].status = "INCONCLUSIVE"
    assert invalid.contract_errors(trace)

    def replay_with_invalid_partial(*_args):
        error = BudgetExceeded("fake_budget")
        error.trace_result = invalid
        raise error

    with pytest.raises(BudgetExceeded) as error:
        run_gate(
            GateRequest(bundle="com.example.hm", workspace=workspace),
            harmony=FakeHarmony(), replay_fn=replay_with_invalid_partial,
        )

    gate = error.value.gate_result
    persisted = TraceResult.from_dict(load_json(os.path.join(
        workspace, "runs", "round_001", trace.trace_id, "result.json")))
    assert persisted.status == "INCONCLUSIVE"
    assert persisted.stop_reason == "INFRA_ERROR"
    assert persisted.divergence.kind == "INFRA_ERROR"
    assert persisted.divergence.detail["attempt"] == "budget"
    assert persisted.divergence.detail["contract_errors"]
    assert persisted.contract_errors(trace) == []
    assert gate.trace_status[trace.trace_id] == "INCONCLUSIVE"
    assert gate.reports[0].failure_type == "INFRA_ERROR"


def test_run_gate_missing_seeds(tmp_path):
    result = run_gate(
        GateRequest(bundle="com.example.hm", workspace=str(tmp_path / "empty")),
        harmony=FakeHarmony(), replay_fn=lambda *a: None,
    )
    assert result.status == "INCONCLUSIVE" and result.stop_reason == "BASELINE_INVALID"
    assert result.consistency_rate is None


def test_gate_result_selected_scope_excludes_explicitly_skipped_not_run_traces():
    result = GateResult(
        passed=True,
        ran_traces=["selected"],
        skipped_traces=["skipped"],
        reports=[],
        flaky=[],
        elapsed_s=0.1,
        trace_status={"selected": "PASS", "skipped": "NOT_RUN"},
    )

    assert result.selected_traces == ["selected"]
    assert result.selected_statuses == {"selected": "PASS"}
    assert result.to_dict()["selected_traces"] == ["selected"]
    assert result.to_dict()["selected_statuses"] == {"selected": "PASS"}


def test_legacy_repair_report_without_cause_is_not_translation_evidence():
    restored = RepairReport.from_dict({
        "trace_id": "legacy",
        "diverged_step": 1,
        "failure_type": "CONTENT_LOSS",
    })

    assert restored.cause_class == "unknown"


def test_interrupted_full_gate_metrics_include_selected_not_run_traces(tmp_path):
    traces = [make_trace(name, [".MainActivity"])
              for name in ("a-passed", "b-inconclusive", "c-pending")]
    workspace = setup_workspace(tmp_path, traces)
    unresolved = make_result(traces[1], passed=False, diverged_step=1,
                             kind="INFRA_ERROR")
    unresolved.status = "INCONCLUSIVE"
    unresolved.stop_reason = "INFRA_ERROR"
    unresolved.divergence.cause_class = "infrastructure"
    unresolved.steps[-1].status = "INCONCLUSIVE"
    unresolved.steps[-1].cause_class = "infrastructure"
    assert unresolved.contract_errors(traces[1]) == []

    result = run_gate(
        GateRequest("com.example.hm", workspace, full_replay=True),
        harmony=FakeHarmony(),
        replay_fn=ReplaySequencer({
            "a-passed": [make_result(traces[0])],
            "b-inconclusive": [unresolved],
        }),
    )

    assert result.status == "INCONCLUSIVE"
    assert result.trace_status["c-pending"] == "NOT_RUN"
    assert result.metrics["traces"] == 3
    assert result.metrics["total_events"] == 3
    assert result.metrics["R_replay"] == pytest.approx(2 / 3, abs=1e-4)
    assert result.metrics["trace_pass_rate"] == pytest.approx(1 / 3, abs=1e-4)
    rows = {row["trace_id"]: row for row in result.metrics["per_trace"]}
    assert rows["c-pending"]["status"] == "NOT_RUN"
    assert rows["c-pending"]["total_steps"] == 1

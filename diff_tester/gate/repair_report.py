"""分叉报告 → 修复环节输入（AI实现参考2 §6）。

字段设计为"修复所需的最小充分信息"：业务场景、分叉步、失败类型、
期望/实际对照、疑似翻译单元、前序步骤摘要、现场附件路径。
`to_prompt()` 渲染为可直接拼进修复 LLM prompt 的中文 Markdown 段落。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional

from ..oracle import PagePairs
from ..schemas import Trace, TraceResult

#: DIVERGENCE_KINDS → 面向修复的失败类型
KIND_TO_FAILURE = {
    "EXEC_UNMAPPED": "ALIGN_FAIL",
    "EXEC_AMBIGUOUS": "ALIGN_FAIL",
    "L0_CRASH": "CRASH",
    "L1_PAGE": "WRONG_PAGE",
    "L2_CONTENT": "CONTENT_LOSS",
}

FAILURE_EXPLAIN = {
    "ALIGN_FAIL": "目标控件在鸿蒙界面上找不到匹配（控件未译出/不可交互）",
    "CRASH": "鸿蒙端进程崩溃或应用退到后台",
    "WRONG_PAGE": "页面跳转逻辑不一致（跳错页/没跳转）",
    "CONTENT_LOSS": "页面内容/值状态不一致（丢文本、状态未同步）",
    "TIMEOUT": "门禁超时（通常意味着鸿蒙端卡死/白屏）",
}

#: expected.must_have_texts / actual 文本列表的上限
_MAX_TEXTS = 15


@dataclass
class RepairReport:
    trace_id: str
    trace_intent: str                    # 种子文件名里的业务描述
    diverged_step: int
    failure_type: str                    # ALIGN_FAIL | CRASH | WRONG_PAGE | CONTENT_LOSS | TIMEOUT
    abstract_event: dict                 # 分叉步的抽象事件（含目标控件指纹）
    expected: dict                       # 安卓基线: {page, must_have_texts, values, list_counts}
    actual: dict                         # 鸿蒙实况: {page, missing_texts, extra_texts, crash_sig}
    suspect_units: list[str] = field(default_factory=list)
    suspect_files: list[str] = field(default_factory=list)  # 崩溃栈定位的 ets 相对路径
    prior_steps_summary: list[str] = field(default_factory=list)
    artifacts: dict = field(default_factory=dict)
    flaky_escalated: bool = False        # 由连续 FLAKY 升级而来

    def signature(self) -> tuple[str, int, str]:
        """同一缺陷的标识（同轨迹、同步骤、同类型）——修复环节用于检测无效修复。"""
        return (self.trace_id, self.diverged_step, self.failure_type)

    # -- 序列化 -------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "trace_intent": self.trace_intent,
            "diverged_step": self.diverged_step,
            "failure_type": self.failure_type,
            "abstract_event": dict(self.abstract_event),
            "expected": dict(self.expected),
            "actual": dict(self.actual),
            "suspect_units": list(self.suspect_units),
            "suspect_files": list(self.suspect_files),
            "prior_steps_summary": list(self.prior_steps_summary),
            "artifacts": dict(self.artifacts),
            "flaky_escalated": self.flaky_escalated,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RepairReport":
        return cls(
            trace_id=d["trace_id"],
            trace_intent=d.get("trace_intent", ""),
            diverged_step=int(d["diverged_step"]),
            failure_type=d["failure_type"],
            abstract_event=dict(d.get("abstract_event", {})),
            expected=dict(d.get("expected", {})),
            actual=dict(d.get("actual", {})),
            suspect_units=list(d.get("suspect_units", [])),
            suspect_files=list(d.get("suspect_files", [])),
            prior_steps_summary=list(d.get("prior_steps_summary", [])),
            artifacts=dict(d.get("artifacts", {})),
            flaky_escalated=bool(d.get("flaky_escalated", False)),
        )

    # -- 渲染给修复 LLM ------------------------------------------------------

    def to_prompt(self) -> str:
        target = (self.abstract_event.get("target") or {})
        action = self.abstract_event.get("action", "?")
        target_text = target.get("text") or target.get("id_hint") or ""
        where = (
            "在应用冷启动阶段（第一个操作执行前）"
            if self.diverged_step == 0
            else f"在第 {self.diverged_step} 步（{action} “{target_text}”）"
        )
        lines = [
            "## 功能一致性验证失败",
            f"业务场景: {self.trace_intent}；{where}发生分叉。",
            f"失败类型: {self.failure_type}"
            f"（{FAILURE_EXPLAIN.get(self.failure_type, '')}）"
            + ("；注意：该缺陷由连续两轮不稳定复现升级而来" if self.flaky_escalated else ""),
            f"- 期望（安卓原应用）: 页面 {self.expected.get('page', '?')}，"
            f"应出现文本 {self.expected.get('must_have_texts', [])}",
            f"- 实际（鸿蒙翻译产物）: 页面 {self.actual.get('page', '?')}，"
            f"缺失文本 {self.actual.get('missing_texts', [])}"
            + (f"；崩溃签名 {self.actual['crash_sig']}"
               if self.actual.get("crash_sig") else ""),
        ]
        if self.expected.get("values"):
            lines.append(f"- 期望值状态: {self.expected['values']}")
        if self.failure_type == "ALIGN_FAIL":
            lines.append(
                "目标控件指纹（安卓端录制）: "
                f"role={target.get('role', '?')}, "
                f"id={target.get('id_hint', '') or '无'}, "
                f"text={target.get('text', '') or '无'}, "
                f"desc={target.get('desc', '') or '无'}, "
                f"归一化位置={target.get('rel_bounds')}")
            lines.append(
                "修复提示: 回放器按 id/文本/位置匹配控件。若安卓控件有 id 而鸿蒙"
                "组件未设置，请给对应 ArkTS 组件补 .id('<与安卓一致的id>')；"
                "外观相同的同类控件（如网格中的格子）必须逐个设置与安卓 "
                "android:id 一致的 id，否则回放器无法区分它们。")
        if self.failure_type == "CRASH":
            stack = self._crash_stack_summary()
            if stack:
                lines.append("崩溃栈（faultlogger，Error message 与 Stacktrace "
                             "指向的应用侧 ets 文件/行号即崩点）:\n```\n"
                             + stack + "\n```")
        if self.suspect_files:
            lines.append(f"崩溃栈定位到的文件: {self.suspect_files}")
        if self.suspect_units:
            lines.append(f"疑似问题单元: {self.suspect_units}")
        if self.prior_steps_summary:
            lines.append("前序步骤(均已通过): " + "；".join(self.prior_steps_summary))
        dump_summary = self._dump_summary()
        if dump_summary:
            lines.append("鸿蒙端当前控件树摘要（截断）:\n```\n" + dump_summary + "\n```")
        lines.append("请修复上述翻译单元中与该交互相关的事件处理/状态更新/页面跳转逻辑。")
        return "\n".join(lines)

    def _crash_stack_summary(self, max_chars: int = 1500) -> str:
        """crash_stack.txt 的头部（Reason/Error message/Stacktrace 都在前面）。"""
        path = self.artifacts.get("crash_stack", "")
        if not path or not os.path.exists(path):
            return ""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(8000)
            # 有效信息从 "Reason:" 开始（前面是设备信息头），到 HiLog 段结束
            start = content.find("Reason:")
            if start > 0:
                content = content[start:]
            cut = content.find("\nHiLog:")
            if cut > 0:
                content = content[:cut]
            return content[:max_chars] + ("…" if len(content) > max_chars else "")
        except OSError:
            return ""

    def _dump_summary(self, max_chars: int = 1200) -> str:
        path = self.artifacts.get("harmony_dump", "")
        if not path or not os.path.exists(path):
            return ""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(max_chars + 1)
            return content[:max_chars] + ("…" if len(content) > max_chars else "")
        except OSError:
            return ""


# ---------------------------------------------------------------------------
# 从回放结果构建报告
# ---------------------------------------------------------------------------

def _intent_of(trace: Trace) -> str:
    """业务描述：优先 meta.intent，其次种子文件名约定 seed_NN_描述。"""
    if trace.meta.get("intent"):
        return str(trace.meta["intent"])
    parts = trace.trace_id.split("_", 2)
    if len(parts) == 3 and not parts[2].isdigit():
        return parts[2]
    return trace.trace_id


def _texts_list(texts: dict[str, int]) -> list[str]:
    out = []
    for k, n in texts.items():
        if k == "<VOLATILE>":
            continue
        out.append(k if n == 1 else f"{k} (x{n})")
    return sorted(out)[:_MAX_TEXTS]


#: 崩溃栈中的应用侧源文件引用，如 "entry/src/main/ets/pages/MainPage.ets:123"
_STACK_ETS_RE = re.compile(r"\(?((?:entry|[\w-]+)/src/main/ets/[\w/.-]+\.ets)(?::\d+)?")


def suspect_files_from_stack(stack_path: str) -> list[str]:
    """从 crash_stack.txt 提取应用侧 ets 相对路径（保持栈中出现顺序、去重）。"""
    if not stack_path or not os.path.exists(stack_path):
        return []
    try:
        with open(stack_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(8000)
    except OSError:
        return []
    cut = content.find("\nHiLog:")
    if cut > 0:
        content = content[:cut]
    seen: list[str] = []
    for m in _STACK_ETS_RE.finditer(content):
        rel = m.group(1)
        if rel not in seen:
            seen.append(rel)
    return seen


def suspect_units_for_page(page: str, unit_page_map: dict[str, dict]) -> list[str]:
    """由页面名反查 unit_page_map（词干归一比较）。"""
    from .selector import GLOBAL_UNIT
    stem = PagePairs._stem(page) if page else ""
    if not stem:
        return []
    units = []
    for unit, entry in unit_page_map.items():
        if unit == GLOBAL_UNIT:
            continue
        for p in entry.get("android_pages", []):
            if p != "*" and PagePairs._stem(p) == stem:
                units.append(unit)
                break
    return sorted(units)


def build_repair_report(
    trace: Trace,
    result: TraceResult,
    unit_page_map: Optional[dict[str, dict]] = None,
) -> RepairReport:
    """从带分叉的 TraceResult 构建修复报告。"""
    div = result.divergence
    assert div is not None, "build_repair_report 需要带分叉的 TraceResult"
    unit_page_map = unit_page_map or {}

    event = next((e for e in trace.events if e.step == div.diverged_step), None)
    a_state, h_state = div.android_state, div.harmony_state

    expected = {
        "page": a_state.page if a_state else "",
        "must_have_texts": _texts_list(a_state.texts) if a_state else [],
        "values": dict(a_state.values) if a_state else {},
        "list_counts": dict(a_state.list_counts) if a_state else {},
    }
    actual = {
        "page": h_state.page if h_state else "",
        "missing_texts": list(div.detail.get("missing_texts", []))[:_MAX_TEXTS],
        "extra_texts": list(div.detail.get("extra_texts", []))[:_MAX_TEXTS],
        "crash_sig": (h_state.crash_sig if h_state else None) or div.detail.get("crash_sig"),
    }

    prior = []
    for rec in result.steps:
        if rec.step >= div.diverged_step:
            continue
        ev = next((e for e in trace.events if e.step == rec.step), None)
        tgt = ""
        if ev is not None and ev.target is not None:
            tgt = ev.target.text or ev.target.id_hint or ""
        prior.append(f"step{rec.step} {rec.action} {tgt} → {rec.expected_page} ✓")

    artifacts = {}
    if a_state and a_state.screenshot:
        artifacts["android_screenshot"] = a_state.screenshot
    if h_state and h_state.screenshot:
        artifacts["harmony_screenshot"] = h_state.screenshot
    if h_state and h_state.dump_path:
        artifacts["harmony_dump"] = h_state.dump_path
    suspect_files: list[str] = []
    if div.artifacts_dir:
        hilog = os.path.join(div.artifacts_dir, "hilog_tail.txt")
        if os.path.exists(hilog):
            artifacts["hilog_tail"] = hilog
        crash_stack = os.path.join(div.artifacts_dir, "crash_stack.txt")
        if os.path.exists(crash_stack):
            artifacts["crash_stack"] = crash_stack
            suspect_files = suspect_files_from_stack(crash_stack)

    return RepairReport(
        trace_id=trace.trace_id,
        trace_intent=_intent_of(trace),
        diverged_step=div.diverged_step,
        failure_type=KIND_TO_FAILURE.get(div.kind, "CONTENT_LOSS"),
        abstract_event=event.to_dict() if event else {},
        expected=expected,
        actual=actual,
        suspect_units=suspect_units_for_page(expected["page"], unit_page_map),
        suspect_files=suspect_files,
        prior_steps_summary=prior,
        artifacts=artifacts,
    )


def build_timeout_report(pending_trace_ids: list[str], elapsed_s: float) -> RepairReport:
    """全轮硬超时报告（§5 预算控制）：通常意味着鸿蒙端卡死/白屏。"""
    return RepairReport(
        trace_id=",".join(pending_trace_ids) or "(unknown)",
        trace_intent="门禁整轮超时，剩余轨迹未回放",
        diverged_step=0,
        failure_type="TIMEOUT",
        abstract_event={},
        expected={},
        actual={"elapsed_s": round(elapsed_s, 1),
                "pending_traces": list(pending_trace_ids)},
    )

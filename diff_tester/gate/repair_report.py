"""分叉报告 → 修复环节输入（AI实现参考2 §6）。

字段设计为"修复所需的最小充分信息"：业务场景、分叉步、失败类型、
期望/实际对照、疑似翻译单元、前序步骤摘要、现场附件路径。
`to_prompt()` 渲染为可直接拼进修复 LLM prompt 的中文 Markdown 段落。
"""
from __future__ import annotations

import os
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
            prior_steps_summary=list(d.get("prior_steps_summary", [])),
            artifacts=dict(d.get("artifacts", {})),
            flaky_escalated=bool(d.get("flaky_escalated", False)),
        )

    # -- 渲染给修复 LLM ------------------------------------------------------

    def to_prompt(self) -> str:
        target = (self.abstract_event.get("target") or {})
        action = self.abstract_event.get("action", "?")
        target_text = target.get("text") or target.get("id_hint") or ""
        lines = [
            "## 功能一致性验证失败",
            f"业务场景: {self.trace_intent}；在第 {self.diverged_step} 步"
            f"（{action} “{target_text}”）发生分叉。",
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
        if self.suspect_units:
            lines.append(f"疑似问题单元: {self.suspect_units}")
        if self.prior_steps_summary:
            lines.append("前序步骤(均已通过): " + "；".join(self.prior_steps_summary))
        dump_summary = self._dump_summary()
        if dump_summary:
            lines.append("鸿蒙端当前控件树摘要（截断）:\n```\n" + dump_summary + "\n```")
        lines.append("请修复上述翻译单元中与该交互相关的事件处理/状态更新/页面跳转逻辑。")
        return "\n".join(lines)

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
    if div.artifacts_dir:
        hilog = os.path.join(div.artifacts_dir, "hilog_tail.txt")
        if os.path.exists(hilog):
            artifacts["hilog_tail"] = hilog

    return RepairReport(
        trace_id=trace.trace_id,
        trace_intent=_intent_of(trace),
        diverged_step=div.diverged_step,
        failure_type=KIND_TO_FAILURE.get(div.kind, "CONTENT_LOSS"),
        abstract_event=event.to_dict() if event else {},
        expected=expected,
        actual=actual,
        suspect_units=suspect_units_for_page(expected["page"], unit_page_map),
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

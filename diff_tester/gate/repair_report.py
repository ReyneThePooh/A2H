"""分叉报告 → 修复环节输入（AI实现参考2 §6）。

字段设计为"修复所需的最小充分信息"：业务场景、分叉步、失败类型、
期望/实际对照、疑似翻译单元、前序步骤摘要、现场附件路径。
`to_prompt()` 渲染为可直接拼进修复 LLM prompt 的中文 Markdown 段落。
"""
from __future__ import annotations

import os
import re
import json
import hashlib
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
    "EXTERNAL_SURFACE": "EXTERNAL_PROTOCOL",  # persisted pre-contract results
    "EXTERNAL_PROTOCOL": "EXTERNAL_PROTOCOL",
    **{kind: kind for kind in ("INFRA_ERROR", "BASELINE_INVALID", "STARTUP_STATE_MISMATCH",
                               "PRECONDITION_MISMATCH", "BUDGET_EXHAUSTED", "UNSTABLE")},
}

FAILURE_EXPLAIN = {
    "ALIGN_FAIL": "目标控件在鸿蒙界面上找不到匹配（控件未译出/不可交互）",
    "CRASH": "鸿蒙端进程崩溃或应用退到后台",
    "WRONG_PAGE": "页面跳转逻辑不一致（跳错页/没跳转）",
    "CONTENT_LOSS": "页面内容/值状态不一致（丢文本、状态未同步）",
    "TIMEOUT": "门禁预算耗尽，不能据此判定应用故障",
    "INFRA_ERROR": "设备或驱动操作失败，不能据此修改业务代码",
    "BASELINE_INVALID": "基线证据缺失或不完整，需要重新录制",
    "STARTUP_STATE_MISMATCH": "操作前启动状态与基线不一致",
    "PRECONDITION_MISMATCH": "动作执行前状态与基线不一致",
    "BUDGET_EXHAUSTED": "运行预算耗尽，剩余验证未完成",
    "UNSTABLE": "界面未稳定，当前无法形成可靠判定",
    "EXTERNAL_PROTOCOL": "目标端系统表面与事件声明的外部表面协议不一致",
    "PLATFORM_MEDIATION": "系统中介表面无法按已知平台规则恢复，不能据此修改应用业务代码",
    "FLAKY": "复跑证据不一致，不能判为通过或自动修复",
}

#: expected.must_have_texts / actual 文本列表的上限
_MAX_TEXTS = 15
_TRANSIENT_EVIDENCE_KEYS = frozenset({
    "artifacts_dir",
    "dump_path",
    "patch_path",
    "run_dir",
    "screenshot",
})


def _canonical_evidence(value):
    """Normalize structured evidence while dropping transient artifact paths."""
    if isinstance(value, dict):
        return {
            str(key): _canonical_evidence(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key) not in _TRANSIENT_EVIDENCE_KEYS
        }
    if isinstance(value, (set, frozenset)):
        items = [_canonical_evidence(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(
            item, sort_keys=True, ensure_ascii=False, separators=(",", ":")))
    if isinstance(value, (list, tuple)):
        return [_canonical_evidence(item) for item in value]
    return value


def _evidence_digest(payload: dict, *, length: Optional[int] = None) -> str:
    """Return a deterministic digest for structured failure evidence."""
    encoded = json.dumps(
        _canonical_evidence(payload),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return digest[:length] if length is not None else digest


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
    phase: str = "compare"
    cause_class: str = "unknown"
    action_executed: bool = False
    root_cause_key: str = ""
    evidence: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.root_cause_key:
            target = self.abstract_event.get("target") or {}
            detail = self.evidence.get("detail", {})
            payload = {"phase": self.phase, "cause_class": self.cause_class,
                       "failure_type": self.failure_type,
                       "failed_predicates": detail.get("failed_predicates", []),
                       "protocol_error": detail.get("protocol_error"),
                       "external_protocol": detail.get("external_protocol"),
                       "expected_page": self.expected.get("page"),
                       "actual_page": self.actual.get("page"),
                       "missing": self.actual.get("missing_texts"),
                       "crash": self.actual.get("crash_sig"),
                       "target": {k: target.get(k) for k in ("id_hint", "role", "text", "desc")}}
            self.root_cause_key = _evidence_digest(payload, length=24)

    def signature(self) -> tuple[str, int, str]:
        """同一缺陷的标识（同轨迹、同步骤、同类型）——修复环节用于检测无效修复。"""
        return (self.trace_id, self.diverged_step, self.failure_type)

    @property
    def confirmation_fingerprint(self) -> str:
        """Strict identity used only to confirm an immediately repeated failure.

        ``root_cause_key`` intentionally stays coarse so related reports can be
        clustered for diagnosis.  Confirmation must retain the complete
        machine evidence: predicate payloads, L2 deltas, and matcher candidates.
        It is computed on access so deserialized or subsequently enriched
        reports cannot carry a stale confirmation identity.
        """
        event = {
            key: self.abstract_event.get(key)
            for key in ("action", "params", "target", "external_surface")
        }
        payload = {
            "schema": 1,
            "trace_id": self.trace_id,
            "diverged_step": self.diverged_step,
            "failure_type": self.failure_type,
            "phase": self.phase,
            "cause_class": self.cause_class or "unknown",
            "action_executed": self.action_executed,
            "event": event,
            "expected": self.expected,
            "actual": self.actual,
            "detail": self.evidence.get("detail", {}),
        }
        return _evidence_digest(payload)

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
            "phase": self.phase, "cause_class": self.cause_class,
            "action_executed": self.action_executed, "root_cause_key": self.root_cause_key,
            "confirmation_fingerprint": self.confirmation_fingerprint,
            "evidence": dict(self.evidence),
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
            phase=d.get("phase", "compare"), cause_class=d.get("cause_class", "unknown"),
            action_executed=bool(d.get("action_executed", False)),
            root_cause_key=d.get("root_cause_key", ""), evidence=dict(d.get("evidence", {})),
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
            f"阶段: {self.phase}；原因类别: {self.cause_class}；动作已执行: {self.action_executed}",
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
            lines.append("先确认动作前页面与控件证据；不能仅凭匹配失败推断缺失 id。")
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
        if self.evidence:
            failed = self.evidence.get("detail", {}).get("failed_predicates", [])
            if failed:
                lines.append("未通过的行为谓词: " + "、".join(failed))
            lines.append("结构化证据: " + json.dumps(self.evidence, ensure_ascii=False))
        dump_summary = self._dump_summary()
        if dump_summary:
            lines.append("鸿蒙端当前控件树摘要（截断）:\n```\n" + dump_summary + "\n```")
        if self.cause_class == "translation":
            lines.append("依据证据检查当前页、入口注册、事件处理及依赖，不得更改基线或降低判定阈值。")
        else:
            lines.append("本报告不构成业务代码缺陷的证据，先解决运行环境或基线问题。")
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
                root = json.load(f)
            nodes = []
            pending = [root]
            target = self.abstract_event.get("target") or {}
            wanted = {str(target.get(k) or "") for k in ("id_hint", "text", "desc")} - {""}
            while pending:
                node = pending.pop()
                if not isinstance(node, dict):
                    continue
                pending.extend(reversed(node.get("children", [])))
                if node.get("clickable") or node.get("editable") or node.get("text") or node.get("desc"):
                    item = {k: node.get(k) for k in ("role", "id", "text", "desc", "clickable", "editable", "checked", "rel_bounds")}
                    priority = bool(wanted.intersection(str(item.get(k) or "") for k in ("id", "text", "desc")))
                    nodes.append((priority, bool(node.get("clickable") or node.get("editable")), item))
            nodes.sort(key=lambda n: (n[0], n[1]), reverse=True)
            selected = []
            for _, _, item in nodes:
                candidate = json.dumps(selected + [item], ensure_ascii=False)
                if len(candidate) > max_chars:
                    continue
                selected.append(item)
            return json.dumps({"nodes": selected, "omitted": len(nodes) - len(selected)}, ensure_ascii=False)
        except (OSError, ValueError, TypeError):
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
    divergence: Optional[DivergenceReport] = None,
) -> RepairReport:
    """从带分叉的 TraceResult 构建修复报告。"""
    div = divergence or result.divergence
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
        if rec.step >= div.diverged_step or not rec.verified or not rec.passed:
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

    suspect_units = suspect_units_for_page(expected["page"], unit_page_map)
    if div.action_executed and event and event.pre_state:
        suspect_units = sorted(set(suspect_units + suspect_units_for_page(event.pre_state.page, unit_page_map)))
    if div.phase in ("reset", "startup") and div.cause_class == "translation":
        suspect_units = sorted(set(suspect_units + ["__global__"]))
    return RepairReport(
        trace_id=trace.trace_id,
        trace_intent=_intent_of(trace),
        diverged_step=div.diverged_step,
        failure_type=KIND_TO_FAILURE.get(div.kind, div.kind),
        abstract_event=event.to_dict() if event else {},
        expected=expected,
        actual=actual,
        suspect_units=suspect_units,
        suspect_files=suspect_files,
        prior_steps_summary=prior,
        artifacts=artifacts,
        phase=div.phase, cause_class=div.cause_class, action_executed=div.action_executed,
        evidence={"detail": div.detail, "confirmed": div.confirmed,
                  "soft_failure": div.soft_failure,
                  "executed_steps": result.executed_steps,
                  "verified_steps": result.verified_steps,
                  "pre_state": event.pre_state.to_dict() if event and event.pre_state else None,
                  "post_state": event.post_state.to_dict() if event and event.post_state else None},
    )


def build_timeout_report(pending_trace_ids: list[str], elapsed_s: float) -> RepairReport:
    """全轮硬超时报告（§5 预算控制）：通常意味着鸿蒙端卡死/白屏。"""
    return RepairReport(
        trace_id=",".join(pending_trace_ids) or "(unknown)",
        trace_intent="门禁整轮超时，剩余轨迹未回放",
        diverged_step=0,
        failure_type="BUDGET_EXHAUSTED",
        abstract_event={},
        expected={},
        actual={"elapsed_s": round(elapsed_s, 1),
                "pending_traces": list(pending_trace_ids)},
        phase="budget", cause_class="budget",
    )


def build_repair_reports(
    trace: Trace,
    result: TraceResult,
    unit_page_map: Optional[dict[str, dict]] = None,
) -> list[RepairReport]:
    """Build one repair report for every observed divergence.

    Historical callers can keep using :func:`build_repair_report`; this helper
    is the explicit multi-failure API used by continued replay.
    """
    reports = list(result.divergences)
    if not reports and result.divergence is not None:
        reports = [result.divergence]
    return [build_repair_report(trace, result, unit_page_map, divergence=item)
            for item in reports]


def cluster_reports(reports: list[RepairReport]) -> dict[str, list[RepairReport]]:
    groups: dict[str, list[RepairReport]] = {}
    for report in reports:
        groups.setdefault(report.root_cause_key, []).append(report)
    return groups

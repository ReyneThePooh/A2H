"""指标计算（§6.7，公式见设计文档 §4.9）。

对轨迹库 {t1..tm}，|tk| 为轨迹长度，dk 为首分叉步号（无分叉 dk=|tk|+1）：
  R_replay  = 已执行动作 / 计划动作       事件可回放率（执行层完成度）
  R_eq_direct = 无中介且通过 L0–L2 的步数 / 有结论的已执行步数
  R_eq_policy = (直接通过步数 + 协议中介后通过步数) / 有结论的已执行步数
  R_eq        = R_eq_policy（兼容旧报告消费者）
  轨迹通过率 = 完整走完的轨迹 / m
  归一化成功前缀深度 = avg(已验证通过步数 / |tk|)
  页面覆盖对齐率、控件召回率（UNMAPPED 计数）、缺陷谱
"""
from __future__ import annotations

from collections import Counter

from .schemas import TraceResult, external_protocol_evidence_errors


def _external_outcome(step) -> tuple[bool, bool, bool, bool]:
    """Return mediated, failed, evaluated, and evidence-present flags.

    External evidence is fail-closed: an unknown or incomplete schema cannot
    claim either mediated success or ordinary direct equivalence.
    """
    evidence = step.external_surface
    evidence_present = evidence is not None
    if not isinstance(evidence, dict):
        return False, False, False, evidence_present

    if external_protocol_evidence_errors(evidence):
        return False, False, False, True

    accepted = evidence.get("accepted")
    attempted = evidence.get("recovery_attempted")
    recovered = evidence.get("recovered")

    mediated = accepted is True and attempted is True and recovered is True
    recovery_failed = (accepted is True and attempted is True and recovered is not True)
    return mediated, recovery_failed, True, True


def compute_metrics(results: list[TraceResult]) -> dict:
    m = len(results)
    sum_len = 0
    sum_replayed = 0        # Σ(dk−1)
    sum_executed = 0        # 已执行步数（状态分叉步已执行、执行分叉步未执行）
    sum_passed_steps = 0    # 通过 L0–L2 的步数
    sum_compared = 0
    sum_policy_evaluated = 0
    sum_direct_verified = 0
    sum_mediated = 0
    sum_external_recovery_failures = 0
    sum_soft_failed_steps = 0
    norm_depths: list[float] = []
    passed_traces = 0

    android_pages: set[str] = set()
    aligned_pages: set[str] = set()
    target_steps = 0
    unmapped_steps = 0

    per_trace: list[dict] = []

    for r in results:
        n = max(1, r.total_steps)
        sum_len += r.total_steps
        sum_replayed += max(0, min(r.executed_steps, r.total_steps))
        sum_executed += r.executed_steps
        compared_steps = 0
        policy_evaluated_steps = 0
        direct_verified_steps = 0
        mediated_steps = 0
        external_recovery_failures = 0
        for step in r.steps:
            if step.step <= 0:
                continue
            compared = step.verified and step.action_executed
            (mediated, recovery_failed, protocol_evaluated,
             external_evidence_present) = _external_outcome(step)
            if compared:
                compared_steps += 1
            if step.action_executed and (compared or protocol_evaluated):
                policy_evaluated_steps += 1
            if recovery_failed and step.action_executed:
                external_recovery_failures += 1
            if step.passed and compared:
                if mediated:
                    mediated_steps += 1
                elif not external_evidence_present:
                    direct_verified_steps += 1
        passed_steps = direct_verified_steps + mediated_steps
        soft_failed_steps = sum(1 for step in r.steps if step.soft_failed)
        sum_compared += compared_steps
        sum_policy_evaluated += policy_evaluated_steps
        sum_passed_steps += passed_steps
        sum_direct_verified += direct_verified_steps
        sum_mediated += mediated_steps
        sum_external_recovery_failures += external_recovery_failures
        sum_soft_failed_steps += soft_failed_steps
        norm_depths.append(min(1.0, passed_steps / n))
        if (r.passed and r.status == "PASS" and r.total_steps > 0
                and passed_steps == r.total_steps and r.executed_steps == r.total_steps):
            passed_traces += 1

        android_pages.update(p for p in r.android_pages if p)
        for s in r.steps:
            if s.expected_page and s.passed and s.verified and s.action_executed:
                aligned_pages.add(s.expected_page)
            if s.match_kind is not None:
                target_steps += 1
                if s.match_kind != "MATCHED":
                    unmapped_steps += 1

        d = r.divergence
        per_trace.append({
            "trace_id": r.trace_id,
            "total_steps": r.total_steps,
            "executed_steps": r.executed_steps,
            "verified_steps": passed_steps,
            "compared_steps": compared_steps,
            "policy_evaluated_steps": policy_evaluated_steps,
            "direct_verified_steps": direct_verified_steps,
            "mediated_steps": mediated_steps,
            "external_recovery_failures": external_recovery_failures,
            "soft_failed_steps": soft_failed_steps,
            "terminal_stop_step": (
                r.steps[-1].step
                if r.stop_reason and r.steps else None
            ),
            "R_eq_direct": _ratio(direct_verified_steps, policy_evaluated_steps),
            "R_eq_policy": _ratio(passed_steps, policy_evaluated_steps),
            "status": r.status,
            "passed": r.passed and r.status == "PASS" and passed_steps == r.total_steps and r.total_steps > 0,
            "first_divergence_step": r.first_divergence_step,
            "kind": d.kind if d else None,
            "category": d.category if d else None,
            "confirmed": d.confirmed if d else None,
        })

    aligned_pages &= android_pages
    all_divergences = [d for r in results for d in (r.divergences or
                                                    ([r.divergence] if r.divergence else []))]
    kinds = Counter(d.kind for d in all_divergences)
    categories = Counter(d.category for d in all_divergences if d.category)
    defect_types = Counter(d.detail.get("defect_type") for d in all_divergences
                           if d.detail.get("defect_type"))

    return {
        "traces": m,
        "total_events": sum_len,
        "executed_steps": sum_executed,
        "verified_steps": sum_passed_steps,
        "compared_steps": sum_compared,
        "policy_evaluated_steps": sum_policy_evaluated,
        "direct_verified_steps": sum_direct_verified,
        "mediated_steps": sum_mediated,
        "external_recovery_failures": sum_external_recovery_failures,
        "soft_failed_steps": sum_soft_failed_steps,
        "R_replay": _ratio(sum_replayed, sum_len),
        "R_eq_direct": _ratio(sum_direct_verified, sum_policy_evaluated),
        "R_eq_policy": _ratio(sum_passed_steps, sum_policy_evaluated),
        "R_eq": _ratio(sum_passed_steps, sum_policy_evaluated),
        "trace_pass_rate": _ratio(passed_traces, m),
        "avg_norm_divergence_depth": round(sum(norm_depths) / m, 4) if m else None,
        "page_coverage_align_rate": _ratio(len(aligned_pages), len(android_pages)),
        "widget_recall": _ratio(target_steps - unmapped_steps, target_steps),
        "unmapped_count": unmapped_steps,
        "divergence_kinds": dict(kinds),
        "defect_categories": dict(categories),
        "defect_spectrum": dict(defect_types),
        "per_trace": per_trace,
    }


def _ratio(a: int, b: int):
    return round(a / b, 4) if b else None

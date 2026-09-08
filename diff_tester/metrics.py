"""指标计算（§6.7，公式见设计文档 §4.9）。

对轨迹库 {t1..tm}，|tk| 为轨迹长度，dk 为首分叉步号（无分叉 dk=|tk|+1）：
  R_replay  = Σ(dk−1) / Σ|tk|            事件可回放率（执行层完成度）
  R_eq      = 通过 L0–L2 的步数 / 已执行步数   状态一致率（行为层完成度）
  轨迹通过率 = 完整走完的轨迹 / m
  归一化首分叉深度 = avg(dk / |tk|)
  页面覆盖对齐率、控件召回率（UNMAPPED 计数）、缺陷谱
"""
from __future__ import annotations

from collections import Counter

from .schemas import TraceResult


def compute_metrics(results: list[TraceResult]) -> dict:
    m = len(results)
    if m == 0:
        return {"traces": 0}

    sum_len = 0
    sum_replayed = 0        # Σ(dk−1)
    sum_executed = 0        # 已执行步数（状态分叉步已执行、执行分叉步未执行）
    sum_passed_steps = 0    # 通过 L0–L2 的步数
    norm_depths: list[float] = []
    passed_traces = 0

    android_pages: set[str] = set()
    aligned_pages: set[str] = set()
    target_steps = 0
    unmapped_steps = 0

    per_trace: list[dict] = []

    for r in results:
        n = max(1, r.total_steps)
        if r.passed or r.first_divergence_step is None:
            dk = r.total_steps + 1
        else:
            dk = r.first_divergence_step
        sum_len += r.total_steps
        sum_replayed += dk - 1
        sum_executed += r.executed_steps
        passed_steps = sum(1 for s in r.steps if s.passed)
        sum_passed_steps += passed_steps
        norm_depths.append(dk / n)
        if r.passed:
            passed_traces += 1

        android_pages.update(p for p in r.android_pages if p)
        for s in r.steps:
            if s.expected_page and s.passed:
                aligned_pages.add(s.expected_page)
            if s.match_kind is not None:
                target_steps += 1
                if s.match_kind == "UNMAPPED":
                    unmapped_steps += 1

        d = r.divergence
        per_trace.append({
            "trace_id": r.trace_id,
            "total_steps": r.total_steps,
            "executed_steps": r.executed_steps,
            "passed": r.passed,
            "first_divergence_step": r.first_divergence_step,
            "kind": d.kind if d else None,
            "category": d.category if d else None,
            "confirmed": d.confirmed if d else None,
        })

    aligned_pages &= android_pages
    kinds = Counter(r.divergence.kind for r in results if r.divergence)
    categories = Counter(r.divergence.category for r in results
                         if r.divergence and r.divergence.category)
    defect_types = Counter(r.divergence.detail.get("defect_type")
                           for r in results
                           if r.divergence and r.divergence.detail.get("defect_type"))

    return {
        "traces": m,
        "total_events": sum_len,
        "R_replay": _ratio(sum_replayed, sum_len),
        "R_eq": _ratio(sum_passed_steps, sum_executed),
        "trace_pass_rate": _ratio(passed_traces, m),
        "avg_norm_divergence_depth": round(sum(norm_depths) / m, 4),
        "page_coverage_align_rate": _ratio(len(aligned_pages), len(android_pages)),
        "widget_recall": _ratio(target_steps - unmapped_steps, target_steps),
        "unmapped_count": unmapped_steps,
        "divergence_kinds": dict(kinds),
        "defect_categories": dict(categories),
        "defect_spectrum": dict(defect_types),
        "per_trace": per_trace,
    }


def _ratio(a: int, b: int) -> float:
    return round(a / b, 4) if b else 1.0

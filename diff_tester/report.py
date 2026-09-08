"""报告生成：report.json（机器读）+ report.md（人读）。"""
from __future__ import annotations

import os
import time

from .schemas import TraceResult, save_json

_METRIC_LABELS = [
    ("traces", "轨迹数"),
    ("total_events", "事件总数"),
    ("R_replay", "事件可回放率 R_replay"),
    ("R_eq", "状态一致率 R_eq"),
    ("trace_pass_rate", "轨迹通过率"),
    ("avg_norm_divergence_depth", "归一化首分叉深度"),
    ("page_coverage_align_rate", "页面覆盖对齐率"),
    ("widget_recall", "控件召回率"),
    ("unmapped_count", "UNMAPPED 计数"),
]

_KIND_ZH = {
    "EXEC_UNMAPPED": "执行分叉·无匹配控件",
    "EXEC_AMBIGUOUS": "执行分叉·匹配歧义",
    "L0_CRASH": "状态分叉·崩溃",
    "L1_PAGE": "状态分叉·页面身份",
    "L2_CONTENT": "状态分叉·语义内容",
}

_CATEGORY_ZH = {
    "TRANSLATION": "翻译缺陷",
    "PLATFORM": "平台固有差异",
    "SOURCE_BUG": "源应用缺陷",
    "NOISE": "环境噪声",
}

_DEFECT_ZH = {
    "crash": "崩溃", "navigation": "导航", "content": "内容", "state": "状态",
}


def write_reports(metrics: dict, results: list[TraceResult], out_dir: str) -> tuple[str, str]:
    """输出 report.json 与 report.md，返回两个路径。"""
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, "report.json")
    md_path = os.path.join(out_dir, "report.md")

    save_json({
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "metrics": metrics,
        "results": [r.to_dict() for r in results],
    }, json_path)

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(_render_md(metrics, results))
    return json_path, md_path


def _render_md(metrics: dict, results: list[TraceResult]) -> str:
    lines: list[str] = []
    lines.append("# 安卓→鸿蒙翻译差分测试报告")
    lines.append("")
    lines.append(f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")

    # ---- 汇总指标 ----
    lines.append("## 一、汇总指标")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("|---|---|")
    for key, label in _METRIC_LABELS:
        if key in metrics:
            lines.append(f"| {label} | {metrics[key]} |")
    lines.append("")

    # ---- 缺陷谱 ----
    if metrics.get("defect_categories") or metrics.get("divergence_kinds"):
        lines.append("## 二、缺陷谱")
        lines.append("")
        cats = metrics.get("defect_categories", {})
        if cats:
            lines.append("**归因分类**：")
            lines.append("")
            lines.append("| 类别 | 计数 |")
            lines.append("|---|---|")
            for k, v in sorted(cats.items(), key=lambda x: -x[1]):
                lines.append(f"| {_CATEGORY_ZH.get(k, k)} | {v} |")
            lines.append("")
        spectrum = metrics.get("defect_spectrum", {})
        if spectrum:
            lines.append("**翻译缺陷四型**：")
            lines.append("")
            lines.append("| 缺陷型 | 计数 |")
            lines.append("|---|---|")
            for k, v in sorted(spectrum.items(), key=lambda x: -x[1]):
                lines.append(f"| {_DEFECT_ZH.get(k, k)} | {v} |")
            lines.append("")
        kinds = metrics.get("divergence_kinds", {})
        if kinds:
            lines.append("**分叉层级分布**：")
            lines.append("")
            lines.append("| 分叉类型 | 计数 |")
            lines.append("|---|---|")
            for k, v in sorted(kinds.items(), key=lambda x: -x[1]):
                lines.append(f"| {_KIND_ZH.get(k, k)} | {v} |")
            lines.append("")

    # ---- 每条轨迹明细 ----
    lines.append("## 三、轨迹明细")
    lines.append("")
    lines.append("| 轨迹 | 步数 | 已执行 | 结果 | 首分叉步 | 分叉类型 | 归因 | artifacts |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in results:
        d = r.divergence
        lines.append("| {} | {} | {} | {} | {} | {} | {} | {} |".format(
            r.trace_id,
            r.total_steps,
            r.executed_steps,
            "通过" if r.passed else "分叉",
            r.first_divergence_step or "-",
            _KIND_ZH.get(d.kind, d.kind) if d else "-",
            _CATEGORY_ZH.get(d.category, d.category or "未归因") if d else "-",
            d.artifacts_dir.replace("\\", "/") if d and d.artifacts_dir else "-",
        ))
    lines.append("")

    # ---- 分叉详情 ----
    diverged = [r for r in results if r.divergence]
    if diverged:
        lines.append("## 四、分叉详情")
        lines.append("")
        for r in diverged:
            d = r.divergence
            lines.append(f"### {r.trace_id} · step {d.diverged_step} · "
                         f"{_KIND_ZH.get(d.kind, d.kind)}")
            lines.append("")
            if d.android_state:
                lines.append(f"- 期望页面（安卓基线）：`{d.android_state.page}`")
            if d.harmony_state:
                lines.append(f"- 实际页面（鸿蒙）：`{d.harmony_state.page}`")
            if d.confirmed is not None:
                lines.append(f"- 复现确认：{'是' if d.confirmed else '否'}"
                             f"（{d.detail.get('reproduced', '-')}）")
            for key in ("jaccard", "widget_align_rate", "score"):
                if key in d.detail:
                    lines.append(f"- {key}: {d.detail[key]}")
            missing = d.detail.get("missing_texts")
            if missing:
                lines.append(f"- 鸿蒙端缺失文本（前若干）：{missing[:8]}")
            if d.detail.get("crash_sig") or (d.harmony_state and d.harmony_state.crash_sig):
                sig = d.detail.get("crash_sig") or d.harmony_state.crash_sig
                lines.append(f"- 崩溃签名：`{sig}`")
            if d.artifacts_dir:
                lines.append(f"- 附件目录：`{d.artifacts_dir}`")
            lines.append("")

    return "\n".join(lines)

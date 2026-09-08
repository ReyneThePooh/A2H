"""去噪与归因（§6.6）：复跑确认 + 四分类。

流程（对每份 DivergenceReport）：
  1. 同轨迹在鸿蒙端复跑 confirm_runs 次 → 复现次数 < confirm_runs → NOISE；
  2. 全复现 → 查平台差异白名单正则（whitelist.yaml）→ PLATFORM；
  3. 安卓端复跑该轨迹亦异常 → SOURCE_BUG；
  4. 否则 → TRANSLATION，并按 kind 映射缺陷四型（崩溃/导航/内容/状态）。

`decide_category` 为纯函数，可离线单测；设备相关的复跑封装在 `attribute_all`。
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import TYPE_CHECKING, Optional

from .config import Config
from .oracle import PagePairs
from .schemas import (KIND_TO_DEFECT_TYPE, DivergenceReport, Trace,
                      TraceResult)

if TYPE_CHECKING:  # pragma: no cover
    from .adapters.android import AndroidAdapter
    from .adapters.harmony import HarmonyAdapter

logger = logging.getLogger("diff_tester")


# ---------------------------------------------------------------------------
# 白名单
# ---------------------------------------------------------------------------

def load_whitelist(path: Optional[str]) -> list[re.Pattern]:
    """whitelist.yaml：{"patterns": [正则, ...]}（权限弹窗文本、系统 UI 组件名等）。"""
    if not path or not os.path.exists(path):
        return []
    import yaml
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return [re.compile(p) for p in data.get("patterns", [])]


def whitelist_hit(report: DivergenceReport, patterns: list[re.Pattern]) -> bool:
    """分叉的关键文本（detail、崩溃签名、两端页面名）命中任一白名单正则。"""
    if not patterns:
        return False
    corpus_parts = [json.dumps(report.detail, ensure_ascii=False)]
    for st in (report.android_state, report.harmony_state):
        if st:
            corpus_parts.append(st.page)
            if st.crash_sig:
                corpus_parts.append(st.crash_sig)
    corpus = "\n".join(corpus_parts)
    return any(p.search(corpus) for p in patterns)


# ---------------------------------------------------------------------------
# 分类决策（纯函数）
# ---------------------------------------------------------------------------

def decide_category(
    reproduced: int,
    confirm_runs: int,
    wl_hit: bool,
    android_also_fails: Optional[bool],
) -> tuple[str, bool]:
    """返回 (category, confirmed)。

    android_also_fails 为 None 表示安卓端复跑未执行（无设备），
    此时无法排除 SOURCE_BUG，按保守策略仍归 TRANSLATION 并在 detail 里标注。
    """
    if reproduced < confirm_runs:
        return "NOISE", False
    if wl_hit:
        return "PLATFORM", True
    if android_also_fails:
        return "SOURCE_BUG", True
    return "TRANSLATION", True


def _same_divergence(a: DivergenceReport, b: Optional[DivergenceReport]) -> bool:
    return b is not None and a.diverged_step == b.diverged_step and a.kind == b.kind


# ---------------------------------------------------------------------------
# 设备相关：复跑确认
# ---------------------------------------------------------------------------

def attribute_all(
    results: list[TraceResult],
    traces: dict[str, Trace],
    cfg: Config,
    page_pairs: PagePairs,
    harmony: Optional["HarmonyAdapter"] = None,
    android: Optional["AndroidAdapter"] = None,
    confirm_runs: int = 3,
    tmp_root: str = "attribution_runs",
) -> None:
    """就地填写每份分叉报告的 confirmed / category / detail.defect_type。

    - harmony 为 None：离线模式，跳过复跑，category 置 None（报告中标注"未归因"）；
    - android 为 None：跳过 SOURCE_BUG 检查（detail 标注）。
    """
    from .replayer import replay

    patterns = load_whitelist(cfg.whitelist_path)

    for result in results:
        report = result.divergence
        if report is None:
            continue
        report.detail["defect_type"] = KIND_TO_DEFECT_TYPE.get(report.kind, "unknown")

        if harmony is None:
            logger.info("[attribute] %s 离线模式：跳过复跑确认", report.trace_id)
            report.detail["attribution_note"] = "offline: 未做复跑确认"
            continue
        trace = traces.get(report.trace_id)
        if trace is None:
            logger.warning("[attribute] 找不到轨迹 %s，跳过", report.trace_id)
            continue

        # 1) 鸿蒙端复跑 confirm_runs 次
        reproduced = 0
        for i in range(confirm_runs):
            rerun_dir = os.path.join(tmp_root, f"{report.trace_id}_confirm{i}")
            r = replay(trace, harmony, page_pairs, cfg, rerun_dir,
                       collect_artifacts=False)
            if _same_divergence(report, r.divergence):
                reproduced += 1
            logger.info("[attribute] %s 复跑 %d/%d：%s", report.trace_id,
                        i + 1, confirm_runs,
                        "复现" if _same_divergence(report, r.divergence) else "未复现")

        # 2) 白名单
        wl = whitelist_hit(report, patterns)

        # 3) 安卓端自复跑（排除源应用缺陷）
        android_fails: Optional[bool] = None
        if android is not None and reproduced >= confirm_runs and not wl:
            android_fails = _android_self_check(trace, android, cfg)

        category, confirmed = decide_category(reproduced, confirm_runs, wl, android_fails)
        report.confirmed = confirmed
        report.category = category
        report.detail["reproduced"] = f"{reproduced}/{confirm_runs}"
        if android_fails is None and android is None:
            report.detail["attribution_note"] = "未提供安卓设备，SOURCE_BUG 未排除"
        logger.info("[attribute] %s → %s (confirmed=%s)",
                    report.trace_id, category, confirmed)


def _android_self_check(trace: Trace, android: "AndroidAdapter",
                        cfg: Config) -> bool:
    """安卓端自回放该轨迹：异常（无法完成/崩溃）→ True（源应用问题）。"""
    from .matcher import match as _match
    from .state import compile_masks

    masks = compile_masks(cfg.mask_patterns)
    try:
        android.reset_app()
        for ev in trace.events:
            tree = android.dump_tree()
            node = None
            if ev.target is not None:
                m = _match(ev.target, tree, ev.action, cfg.matcher)
                if m.kind != "MATCHED":
                    return True
                node = m.node
            android.execute(ev, node)
            android.wait_stable()
            if android.poll_crash() or not android.app_alive():
                return True
        return False
    except Exception as e:
        logger.warning("[attribute] 安卓自复跑异常: %s", e)
        return True

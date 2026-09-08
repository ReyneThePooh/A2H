"""目标端逐步回放 + 分叉管理（§6.5）。

对每条轨迹逐事件：dump 鸿蒙树 → 控件对齐 → 原生执行 → 等稳定 →
状态抽象 → 分层预言比较；任一环节失败即记录首分叉并终止本轨迹。
"""
from __future__ import annotations

import logging
import os
import shutil
from typing import TYPE_CHECKING, Optional

from .config import Config
from .matcher import match
from .oracle import PagePairs, compare
from .schemas import (DivergenceReport, StepRecord, Trace, TraceResult,
                      save_json)
from .state import alpha, compile_masks

if TYPE_CHECKING:  # pragma: no cover
    from .adapters.harmony import HarmonyAdapter

logger = logging.getLogger("diff_tester")


def replay(
    trace: Trace,
    harmony: "HarmonyAdapter",
    page_pairs: PagePairs,
    cfg: Config,
    results_root: str,
    collect_artifacts: bool = True,
) -> TraceResult:
    """回放一条轨迹，返回 TraceResult（含首分叉报告）。"""
    masks = compile_masks(cfg.mask_patterns)
    trace_dir = os.path.join(results_root, trace.trace_id)
    os.makedirs(trace_dir, exist_ok=True)

    result = TraceResult(
        trace_id=trace.trace_id,
        total_steps=len(trace.events),
        executed_steps=0,
        passed=True,
        android_pages=sorted({ev.post_state.page for ev in trace.events
                              if ev.post_state and ev.post_state.page}),
    )

    harmony.reset_app()

    for ev in trace.events:
        step_dir = os.path.join(trace_dir, f"step{ev.step}")
        rec = StepRecord(
            step=ev.step, action=ev.action,
            expected_page=ev.post_state.page if ev.post_state else "",
        )

        # ---- 对齐（有 target 的事件）---------------------------------------
        node = None
        if ev.target is not None:
            try:
                tree = harmony.dump_tree()
            except Exception as e:
                kind = "EXEC_UNMAPPED"
                logger.warning("[replay] %s step=%d dump 失败: %s",
                               trace.trace_id, ev.step, e)
                rec.verdict_kind, rec.passed = kind, False
                result.steps.append(rec)
                _finalize_divergence(result, trace, ev, kind,
                                     {"error": f"dump_tree: {e}"},
                                     None, harmony, masks, step_dir,
                                     collect_artifacts)
                return result
            screen_png: Optional[str] = None
            if ev.target.patch_path and os.path.exists(ev.target.patch_path):
                screen_png = os.path.join(step_dir, "match_screen.png")
                try:
                    harmony.screenshot(screen_png)
                except Exception:
                    screen_png = None
            m = match(ev.target, tree, ev.action, cfg.matcher, screen_png)
            rec.match_kind, rec.match_score = m.kind, round(m.score, 4)
            if m.kind != "MATCHED":
                kind = "EXEC_UNMAPPED" if m.kind == "UNMAPPED" else "EXEC_AMBIGUOUS"
                logger.warning("[replay] %s step=%d 执行分叉 %s score=%.2f",
                               trace.trace_id, ev.step, kind, m.score)
                rec.verdict_kind, rec.passed = kind, False
                result.steps.append(rec)
                _finalize_divergence(result, trace, ev, kind,
                                     {"match": m.detail,
                                      "score": round(m.score, 4),
                                      "second_score": round(m.second_score, 4),
                                      "target": ev.target.to_dict()},
                                     None, harmony, masks, step_dir,
                                     collect_artifacts)
                return result
            node = m.node

        # ---- 执行 + 等稳定 ---------------------------------------------------
        try:
            harmony.execute(ev, node)
        except Exception as e:
            kind = "EXEC_UNMAPPED"
            logger.warning("[replay] %s step=%d 原生执行失败: %s",
                           trace.trace_id, ev.step, e)
            rec.verdict_kind, rec.passed = kind, False
            result.steps.append(rec)
            _finalize_divergence(result, trace, ev, kind,
                                 {"error": str(e)}, None, harmony, masks,
                                 step_dir, collect_artifacts)
            return result
        rec.unstable = not harmony.wait_stable()
        result.executed_steps += 1

        # ---- 状态抽象 + 分层预言 ----------------------------------------------
        actual = alpha(harmony, masks, step_dir, "harmony")
        if actual.page:
            result.harmony_pages.append(actual.page)

        if ev.post_state is None:      # 种子事件可能未带基线 → 跳过比较
            verdict_str = "SKIP(no-baseline)"
        else:
            verdict = compare(ev.post_state, actual, page_pairs, cfg.oracle)
            if not verdict.passed:
                rec.verdict_kind, rec.passed = verdict.kind, False
                result.steps.append(rec)
                logger.warning("[replay] %s step=%d 状态分叉 %s detail=%s",
                               trace.trace_id, ev.step, verdict.kind,
                               _short(verdict.detail))
                detail = dict(verdict.detail)
                if rec.unstable:
                    detail["unstable"] = True   # 归因阶段降低该步置信度
                _finalize_divergence(result, trace, ev, verdict.kind, detail,
                                     actual, harmony, masks, step_dir,
                                     collect_artifacts)
                return result
            verdict_str = "PASS"

        result.steps.append(rec)
        logger.info("[replay] %s step=%d action=%s match=%s score=%s verdict=%s",
                    trace.trace_id, ev.step, ev.action,
                    rec.match_kind or "-",
                    f"{rec.match_score:.2f}" if rec.match_score is not None else "-",
                    verdict_str)

    logger.info("[replay] %s 全部 %d 步通过", trace.trace_id, len(trace.events))
    return result


def _finalize_divergence(
    result: TraceResult,
    trace: Trace,
    ev,
    kind: str,
    detail: dict,
    actual,
    harmony: "HarmonyAdapter",
    masks: list,
    step_dir: str,
    collect_artifacts: bool,
) -> None:
    """填写分叉报告并收集 artifacts（两端截图、鸿蒙 dump、日志尾 200 行）。"""
    result.passed = False
    result.first_divergence_step = ev.step

    if collect_artifacts:
        os.makedirs(step_dir, exist_ok=True)
        # 鸿蒙端现场（执行分叉时 actual 为空，需现场补采）
        if actual is None:
            try:
                actual = alpha(harmony, masks, step_dir, "harmony")
            except Exception as e:
                logger.warning("补采鸿蒙状态失败: %s", e)
        try:
            with open(os.path.join(step_dir, "hilog_tail.txt"), "w",
                      encoding="utf-8") as f:
                f.write(harmony.log_tail(200))
        except Exception as e:
            logger.warning("收集鸿蒙日志失败: %s", e)
        # 安卓端基线附件（拷贝录制时的截图/dump）
        if ev.post_state:
            for src, name in ((ev.post_state.screenshot, "android_baseline.png"),
                              (ev.post_state.dump_path, "android_baseline_dump.json")):
                if src and os.path.exists(src):
                    try:
                        shutil.copyfile(src, os.path.join(step_dir, name))
                    except OSError:
                        pass

    report = DivergenceReport(
        trace_id=trace.trace_id,
        diverged_step=ev.step,
        kind=kind,
        detail=detail,
        android_state=ev.post_state,
        harmony_state=actual,
        artifacts_dir=step_dir if collect_artifacts else "",
    )
    result.divergence = report
    if collect_artifacts:
        save_json(report.to_dict(), os.path.join(step_dir, "divergence.json"))


def _short(d: dict, limit: int = 200) -> str:
    import json
    s = json.dumps(d, ensure_ascii=False)
    return s[:limit] + ("…" if len(s) > limit else "")

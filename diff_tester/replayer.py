"""目标端逐步回放 + 分叉管理（§6.5）。

对每条轨迹逐事件：dump 鸿蒙树 → 控件对齐 → 原生执行 → 等稳定 →
状态抽象 → 分层预言比较；任一环节失败即记录首分叉并终止本轨迹。
"""
from __future__ import annotations

import logging
import os
import shutil
from typing import TYPE_CHECKING
from run_control import BudgetExceeded, check_budget, consume_budget

from .adapters.base import LaunchCrashError
from .config import Config
from .matcher import match
from .oracle import PagePairs, compare
from .schemas import (EXTERNAL_PROTOCOL_EVIDENCE_SCHEMA_VERSION, AbstractEvent,
                      DivergenceReport, StepRecord, Trace, TraceResult, save_json)
from .state import UNCONFIRMED_EXTERNAL_SURFACE, alpha, compile_masks

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
    """Replay only complete baselines; preserve the phase and actual execution count."""
    masks = compile_masks(cfg.mask_patterns)
    import re
    if not re.fullmatch(r"[\w.-]+", trace.trace_id) or trace.trace_id in (".", ".."):
        raise ValueError("Trace ID is not a safe artifact directory name")
    trace_dir = os.path.join(results_root, trace.trace_id)
    os.makedirs(trace_dir, exist_ok=True)
    result = TraceResult(trace.trace_id, len(trace.events), 0, False, status="NOT_RUN",
                         android_pages=sorted({s.page for s in
                             [trace.initial_state] + [e.post_state for e in trace.events] if s}))
    rec = StepRecord(0, "launch", phase="baseline")
    expected, actual = trace.initial_state, None
    step_dir = os.path.join(trace_dir, "step0")

    def fail(kind, detail, cause="translation", status="FAIL"):
        rec.passed, rec.status, rec.verdict_kind = False, status, kind
        rec.cause_class = cause
        if not result.steps or result.steps[-1] is not rec:
            result.steps.append(rec)
        result.status, result.stop_reason = status, kind
        enriched = dict(detail, phase=rec.phase, cause_class=cause,
                        action_executed=rec.action_executed)
        if rec.external_surface:
            enriched.setdefault("external_protocol", dict(rec.external_surface))
        _finalize_divergence(result, trace, rec.step, expected, kind, enriched, actual,
                             harmony, masks, step_dir,
                             collect_artifacts and cause == "translation")
        return result

    def fail_undeclared_external_surface():
        ownership_unknown = actual.external_surface == UNCONFIRMED_EXTERNAL_SURFACE
        rec.external_surface = _external_protocol_evidence(
            None, actual.external_surface, accepted=False)
        return fail(
            "EXTERNAL_PROTOCOL",
            _external_protocol_failure(
                rec.external_surface,
                ("external_protocol.ownership" if ownership_unknown
                 else "external_protocol.declaration"),
                ("external surface ownership could not be confirmed"
                 if ownership_unknown else
                 "system surface appeared without an event contract"),
            ),
            "unknown" if ownership_unknown else "baseline",
            "INCONCLUSIVE",
        )

    try:
        check_budget()
        errors = trace.baseline_errors()
        if errors:
            return fail("BASELINE_INVALID", {"errors": errors}, "baseline", "INCONCLUSIVE")
        rec.phase = "reset"
        harmony.reset_app()
        check_budget()
        rec.phase = "startup"
        actual = alpha(harmony, masks, step_dir if collect_artifacts else None, "harmony")
        if actual.external_surface is not None:
            return fail_undeclared_external_surface()
        verdict = compare(expected, actual, page_pairs, cfg.oracle, masks=masks)
        if not verdict.passed:
            kind = "L0_CRASH" if verdict.kind == "L0_CRASH" else "STARTUP_STATE_MISMATCH"
            return fail(kind, dict(verdict.detail, oracle_kind=verdict.kind))

        for ev in trace.events:
            check_budget()
            consume_budget("steps")
            rec = StepRecord(ev.step, ev.action, expected_page=ev.post_state.page,
                             phase="precondition")
            step_dir = os.path.join(trace_dir, f"step{ev.step}")
            expected, actual = ev.pre_state, None
            actual = alpha(harmony, masks, step_dir if collect_artifacts else None, "harmony_pre")
            if actual.external_surface is not None:
                return fail_undeclared_external_surface()
            verdict = compare(expected, actual, page_pairs, cfg.oracle, masks=masks)
            if not verdict.passed:
                kind = "L0_CRASH" if verdict.kind == "L0_CRASH" else "PRECONDITION_MISMATCH"
                return fail(kind, dict(verdict.detail, oracle_kind=verdict.kind))

            rec.phase = "match"
            node = None
            if ev.target is not None:
                tree = harmony.dump_tree()
                screen_png = None
                if ev.target.patch_path and os.path.exists(ev.target.patch_path):
                    screen_png = os.path.join(step_dir, "match_screen.png")
                    try:
                        harmony.screenshot(screen_png)
                    except BudgetExceeded:
                        raise
                    except Exception:
                        screen_png = None
                m = match(ev.target, tree, ev.action, cfg.matcher, screen_png)
                rec.match_kind, rec.match_score = m.kind, round(m.score, 4)
                if m.kind != "MATCHED":
                    return fail("EXEC_UNMAPPED" if m.kind == "UNMAPPED" else "EXEC_AMBIGUOUS",
                                {"match": m.detail, "score": m.score,
                                 "second_score": m.second_score, "target": ev.target.to_dict()})
                node = m.node

            rec.phase = "execute"
            check_budget()
            harmony.execute(ev, node)
            rec.action_executed = True
            result.executed_steps += 1
            rec.phase = "settle"
            rec.unstable = not harmony.wait_stable()
            expected = ev.post_state
            if rec.unstable:
                return fail("UNSTABLE", {"reason": "state did not stabilize"}, "unknown", "INCONCLUSIVE")
            rec.phase = "compare"
            actual = alpha(harmony, masks, step_dir if collect_artifacts else None, "harmony")
            if actual.page:
                result.harmony_pages.append(actual.page)
            contract = ev.external_surface
            detected_surface = actual.external_surface
            if contract is None and detected_surface is not None:
                return fail_undeclared_external_surface()

            if contract is not None:
                rec.external_surface = _external_protocol_evidence(
                    contract, detected_surface, accepted=False)
                if detected_surface == UNCONFIRMED_EXTERNAL_SURFACE:
                    detail = _external_protocol_failure(
                        rec.external_surface,
                        "external_protocol.ownership",
                        "external surface ownership could not be confirmed",
                    )
                    detail["ownership_observation"] = {
                        "actual_page": actual.page,
                        "application_page_confirmed": False,
                    }
                    return fail(
                        "EXTERNAL_PROTOCOL",
                        detail,
                        "unknown",
                        "INCONCLUSIVE",
                    )
                if detected_surface is None:
                    app_owned = bool(
                        actual.page
        and page_pairs.corresponds(expected.page, actual.page)
                    )
                    detail = _external_protocol_failure(
                        rec.external_surface,
                        "external_protocol.surface",
                        ("declared external surface did not appear"
                         if app_owned else
                         "external surface ownership could not be confirmed"),
                    )
                    detail["ownership_observation"] = {
                        "actual_page": actual.page,
                        "application_page_confirmed": app_owned,
                    }
                    return fail(
                        "EXTERNAL_PROTOCOL",
                        detail,
                        "translation" if app_owned else "unknown",
                        "FAIL" if app_owned else "INCONCLUSIVE",
                    )
                if detected_surface != contract.surface:
                    return fail(
                        "EXTERNAL_PROTOCOL",
                        _external_protocol_failure(
                            rec.external_surface,
                            "external_protocol.surface",
                            "detected external surface does not match the contract",
                        ),
                    )
                rec.external_surface["accepted"] = True
                rec.external_surface["recovery_attempted"] = True
                rec.phase = "recovery"
                try:
                    harmony.execute(
                        AbstractEvent(ev.step, contract.recovery_action, None), None)
                except BudgetExceeded:
                    raise
                except Exception as exc:
                    rec.external_surface.update({
                        "recovered": False,
                        "recovery_error": str(exc),
                        "recovery_error_type": type(exc).__name__,
                    })
                    return fail(
                        "EXTERNAL_PROTOCOL",
                        _external_protocol_failure(
                            rec.external_surface,
                            "external_protocol.recovery",
                            "external surface recovery action failed",
                        ),
                        "platform",
                        "INCONCLUSIVE",
                    )
                try:
                    recovery_stable = harmony.wait_stable()
                except BudgetExceeded:
                    raise
                except Exception as exc:
                    rec.external_surface.update({
                        "recovered": False,
                        "recovery_error": str(exc),
                        "recovery_error_type": type(exc).__name__,
                    })
                    return fail(
                        "EXTERNAL_PROTOCOL",
                        _external_protocol_failure(
                            rec.external_surface,
                            "external_protocol.recovery",
                            "external surface stability check failed",
                        ),
                        "platform",
                        "INCONCLUSIVE",
                    )
                if not recovery_stable:
                    rec.external_surface.update({
                        "recovered": False,
                        "recovery_error": (
                            "external surface did not stabilize after "
                            f"{contract.recovery_action}"
                        ),
                    })
                    return fail(
                        "EXTERNAL_PROTOCOL",
                        _external_protocol_failure(
                            rec.external_surface,
                            "external_protocol.recovery",
                            rec.external_surface["recovery_error"],
                        ),
                        "platform",
                        "INCONCLUSIVE",
                    )
                actual = alpha(
                    harmony,
                    masks,
                    step_dir if collect_artifacts else None,
                    "harmony_recovered",
                )
                if actual.external_surface is not None:
                    rec.external_surface.update({
                        "recovered": False,
                        "actual_after_recovery": actual.external_surface,
                        "recovery_error": "external surface remained after recovery",
                    })
                    return fail(
                        "EXTERNAL_PROTOCOL",
                        _external_protocol_failure(
                            rec.external_surface,
                            "external_protocol.recovery",
                            rec.external_surface["recovery_error"],
                        ),
                        "platform",
                        "INCONCLUSIVE",
                    )
                rec.external_surface["recovered"] = True
                rec.phase = "compare"

            verdict = compare(expected, actual, page_pairs, cfg.oracle, masks=masks)
            rec.verified = True
            if not verdict.passed:
                if _is_soft_content_failure(verdict, rec, expected, actual, page_pairs):
                    # The action did execute and the target page is correct. Keep
                    # the content evidence, then let the next event exercise the
                    # rest of the trace. Precondition/content failures remain
                    # hard stops because they invalidate the next action's state.
                    rec.soft_failed = True
                    rec.failure_detail = dict(verdict.detail)
                    rec.cause_class = "translation"
                    rec.status = "FAIL"
                    rec.verdict_kind = "L2_CONTENT"
                    detail = dict(verdict.detail, soft_failure=True,
                                  phase=rec.phase, cause_class=rec.cause_class,
                                  action_executed=rec.action_executed)
                    _finalize_divergence(
                        result, trace, rec.step, expected, "L2_CONTENT", detail,
                        actual, harmony, masks, step_dir,
                        collect_artifacts and rec.cause_class == "translation",
                        soft_failure=True,
                    )
                    result.steps.append(rec)
                    result.soft_failed_steps.append(rec.step)
                    continue
                return fail(verdict.kind, verdict.detail)
            result.verified_steps += 1
            rec.passed, rec.status = True, "PASS"
            result.steps.append(rec)
        check_budget()
        if result.divergences:
            # A complete replay with one or more soft content mismatches is a
            # reproducible failure, but it has no terminal stop reason.
            result.passed, result.status = False, "FAIL"
            result.stop_reason = ""
        else:
            result.passed, result.status = True, "PASS"
        return result
    except LaunchCrashError as exc:
        return fail("L0_CRASH", {"alive": exc.alive, "crash_sig": exc.crash_sig})
    except BudgetExceeded as exc:
        fail("BUDGET_EXHAUSTED", {"error": str(exc)}, "budget", "INCONCLUSIVE")
        save_json(result.to_dict(), os.path.join(trace_dir, "result.json"))
        exc.trace_result = result
        raise
    except Exception as exc:
        return fail("INFRA_ERROR", {"error": str(exc), "error_type": type(exc).__name__},
                    "infrastructure", "INCONCLUSIVE")


def _external_protocol_evidence(contract, actual_surface, *, accepted: bool) -> dict:
    """Build stable, machine-readable evidence for one protocol decision."""
    return {
        "id": "external_protocol",
        "schema_version": EXTERNAL_PROTOCOL_EVIDENCE_SCHEMA_VERSION,
        "declared_surface": contract.surface if contract else None,
        "actual_surface": actual_surface,
        "ownership": (contract.ownership if contract else
                      "unknown" if actual_surface == UNCONFIRMED_EXTERNAL_SURFACE
                      else "system"),
        "policy": contract.policy if contract else None,
        "accepted": accepted,
        "recovery_attempted": False,
        "recovered": False,
        "recovery_action": contract.recovery_action if contract else None,
    }


def _external_protocol_failure(evidence: dict, predicate_id: str, reason: str) -> dict:
    """Describe a failed protocol predicate without changing step evidence."""
    predicate = {
        "id": predicate_id,
        "passed": False,
        "expected": evidence.get("declared_surface"),
        "actual": evidence.get("actual_surface"),
        "reason": reason,
    }
    return {
        "external_protocol": dict(evidence),
        "failed_predicates": [predicate_id],
        "predicates": [predicate],
        "protocol_error": reason,
    }


def _is_soft_content_failure(verdict, rec: StepRecord, expected, actual,
                             page_pairs: PagePairs) -> bool:
    """Return whether a post-action content mismatch can safely continue.

    Oracle L2 is intentionally narrowed to an application page that already
    passed L0/L1.  The explicit checks here keep future oracle changes from
    accidentally softening crashes, wrong pages, matcher failures, or
    precondition mismatches.
    """
    return bool(
        verdict.kind == "L2_CONTENT"
        and rec.phase == "compare"
        and rec.action_executed
        and expected is not None
        and actual is not None
        and page_pairs.corresponds(expected.page, actual.page)
    )
def _finalize_divergence(
    result: TraceResult,
    trace: Trace,
    step: int,
    post_state,
    kind: str,
    detail: dict,
    actual,
    harmony: "HarmonyAdapter",
    masks: list,
    step_dir: str,
    collect_artifacts: bool,
    soft_failure: bool = False,
) -> None:
    """填写分叉报告并收集 artifacts（两端截图、鸿蒙 dump、日志尾 200 行）。

    step=0 表示启动阶段分叉（无对应事件/基线）。
    """
    result.passed = False
    if result.first_divergence_step is None:
        result.first_divergence_step = step

    if collect_artifacts:
        os.makedirs(step_dir, exist_ok=True)
        # 鸿蒙端现场（执行分叉时 actual 为空，需现场补采）
        if actual is None:
            try:
                actual = alpha(harmony, masks, step_dir, "harmony")
            except BudgetExceeded:
                raise
            except Exception as e:
                logger.warning("补采鸿蒙状态失败: %s", e)

    # 分叉定性校正：执行类分叉若源于进程死亡/崩溃，实为 L0_CRASH。
    # （启动崩溃在 replay() 入口已单独处理；这里兜住轨迹中途的崩溃）
    if kind in ("EXEC_UNMAPPED", "EXEC_AMBIGUOUS"):
        crash_sig, alive = None, True
        try:
            if actual is not None:
                crash_sig, alive = actual.crash_sig, actual.alive
            else:
                crash_sig, alive = harmony.poll_crash(), harmony.app_alive()
        except Exception as e:
            if isinstance(e, BudgetExceeded):
                raise
            logger.warning("崩溃改判检查失败: %s", e)
        if crash_sig or not alive:
            detail = dict(detail)
            detail.update({"alive": alive, "crash_sig": crash_sig,
                           "reclassified_from": kind})
            kind = "L0_CRASH"
            if result.steps:
                result.steps[-1].verdict_kind = kind
            logger.warning("[replay] %s step=%d 改判为 L0_CRASH"
                           "（alive=%s, crash_sig=%s）",
                           trace.trace_id, step, alive, crash_sig)

    if collect_artifacts:
        # 崩溃栈落盘（修复报告直接引用，免得 LLM 对着 hilog 噪音猜）
        crash_sig = detail.get("crash_sig")
        if kind == "L0_CRASH" and crash_sig:
            try:
                content = harmony.read_fault(crash_sig)
                if content:
                    with open(os.path.join(step_dir, "crash_stack.txt"), "w",
                              encoding="utf-8") as f:
                        f.write(content)
            except Exception as e:
                if isinstance(e, BudgetExceeded):
                    raise
                logger.warning("读取崩溃文件失败: %s", e)
        try:
            with open(os.path.join(step_dir, "hilog_tail.txt"), "w",
                      encoding="utf-8") as f:
                f.write(harmony.log_tail(200))
        except Exception as e:
            if isinstance(e, BudgetExceeded):
                raise
            logger.warning("收集鸿蒙日志失败: %s", e)
        # 安卓端基线附件（拷贝录制时的截图/dump）
        if post_state:
            for src, name in ((post_state.screenshot, "android_baseline.png"),
                              (post_state.dump_path, "android_baseline_dump.json")):
                if src and os.path.exists(src):
                    try:
                        shutil.copyfile(src, os.path.join(step_dir, name))
                    except OSError:
                        pass

    report = DivergenceReport(
        trace_id=trace.trace_id,
        diverged_step=step,
        kind=kind,
        detail=detail,
        android_state=post_state,
        harmony_state=actual,
        artifacts_dir=step_dir if collect_artifacts else "",
        phase=detail.get("phase", "compare"),
        cause_class=detail.get("cause_class", "unknown"),
        action_executed=bool(detail.get("action_executed", False)),
        soft_failure=soft_failure,
    )
    result.divergences.append(report)
    if result.divergence is None:
        # Compatibility for all existing consumers. New consumers should use
        # ``divergences`` when they need every observed failure.
        result.divergence = report
    if collect_artifacts:
        save_json(report.to_dict(), os.path.join(step_dir, "divergence.json"))


def _short(d: dict, limit: int = 200) -> str:
    import json
    s = json.dumps(d, ensure_ascii=False)
    return s[:limit] + ("…" if len(s) > limit else "")

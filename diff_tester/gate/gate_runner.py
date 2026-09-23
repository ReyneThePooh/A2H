"""Evidence-preserving differential gate; ambiguous and untested runs never pass."""
from __future__ import annotations

import glob
import logging
import os
import re
import time
import tempfile
from dataclasses import dataclass, field
from typing import Callable, Optional

from run_control import (BudgetExceeded, FileLock, check_budget, consume_budget,
                         deadline_scope, file_sha256)
from ..config import Config
from ..metrics import compute_metrics
from ..oracle import PagePairs
from ..schemas import (DivergenceReport, StepRecord, Trace, TraceResult,
                       load_json, save_json)
from .flaky import ESCALATE_AFTER, FlakyList
from .repair_report import (RepairReport, build_repair_reports,
                            build_timeout_report)
from .selector import select_traces

logger = logging.getLogger("diff_tester")
ReplayFn = Callable[..., TraceResult]


@dataclass
class GateRequest:
    bundle: str
    workspace: str
    changed_units: list[str] = field(default_factory=list)
    hap_path: Optional[str] = None
    harmony_device: Optional[str] = None
    hdc_path: Optional[str] = None
    full_replay: bool = False
    seeds_dir: Optional[str] = None
    config_yaml: Optional[str] = None
    time_budget_s: Optional[float] = None
    deployment_metadata: dict = field(default_factory=dict)
    confirm_failures: bool = True
    trace_ids: list[str] = field(default_factory=list)


@dataclass
class GateResult:
    passed: bool
    ran_traces: list[str]
    skipped_traces: list[str]
    reports: list[RepairReport]
    flaky: list[str]
    elapsed_s: float
    round_no: int = 0
    trace_passed: dict[str, bool] = field(default_factory=dict)
    status: str = ""
    trace_status: dict[str, str] = field(default_factory=dict)
    stop_reason: str = ""
    metrics: dict = field(default_factory=dict)
    deployment_metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.status:
            self.status = "PASS" if self.passed else "FAIL"

    @property
    def consistency_rate(self) -> Optional[float]:
        """Decisive trace outcomes only; report coverage separately."""
        if self.trace_status:
            comparable = [s for s in self.trace_status.values() if s in ("PASS", "FAIL")]
            return comparable.count("PASS") / len(comparable) if comparable else None
        return (sum(self.trace_passed.values()) / len(self.trace_passed)
                if self.trace_passed else None)

    @property
    def selected_traces(self) -> list[str]:
        """Traces in this gate's verification scope, including unattempted ones.

        ``trace_status`` contains the whole seed corpus. A focused replay leaves
        deliberately skipped traces as NOT_RUN, so callers must not interpret
        those entries as incomplete evidence for the selected scope.
        """
        if not self.trace_status:
            return list(self.ran_traces)
        skipped = set(self.skipped_traces)
        return [trace_id for trace_id in self.trace_status if trace_id not in skipped]

    @property
    def selected_statuses(self) -> dict[str, str]:
        return {trace_id: self.trace_status.get(trace_id, "NOT_RUN")
                for trace_id in self.selected_traces}

    def to_dict(self) -> dict:
        rate = self.consistency_rate
        return {"passed": self.passed, "round_no": self.round_no,
                "ran_traces": list(self.ran_traces), "skipped_traces": list(self.skipped_traces),
                "selected_traces": self.selected_traces,
                "selected_statuses": self.selected_statuses,
                "reports": [r.to_dict() for r in self.reports], "flaky": list(self.flaky),
                "elapsed_s": round(self.elapsed_s, 1), "trace_passed": dict(self.trace_passed),
                "consistency_rate": round(rate, 4) if rate is not None else None,
                "status": self.status, "trace_status": dict(self.trace_status),
                "stop_reason": self.stop_reason, "metrics": self.metrics,
                "deployment_metadata": self.deployment_metadata}


def load_seeds(seeds_dir: str) -> list[Trace]:
    return [Trace.from_dict(load_json(f))
            for f in sorted(glob.glob(os.path.join(seeds_dir, "*.json")))]


def _next_round_no(history_dir: str) -> int:
    numbers = []
    paths = glob.glob(os.path.join(history_dir, "round_*.json"))
    paths += glob.glob(os.path.join(os.path.dirname(history_dir), "runs", "round_*"))
    for path in paths:
        match = re.fullmatch(r"round_(\d+)(?:\.json)?", os.path.basename(path))
        if match:
            numbers.append(int(match.group(1)))
    return max(numbers, default=0) + 1


def _unresolved(trace, kind, phase, cause, detail):
    return TraceResult(trace.trace_id, len(trace.events), 0, False,
                       status="INCONCLUSIVE", stop_reason=kind,
                       first_divergence_step=0,
                       divergence=DivergenceReport(trace.trace_id, 0, kind, detail,
                           trace.initial_state, None, phase=phase, cause_class=cause),
                       steps=[StepRecord(
                           0, "launch", verdict_kind=kind, phase=phase,
                           cause_class=cause, status="INCONCLUSIVE",
                       )])


def _contract_checked_result(trace, raw_result, attempt):
    """Normalize replay output and quarantine internally inconsistent evidence."""
    result = TraceResult.from_dict(raw_result.to_dict())
    errors = result.contract_errors(trace)
    if not errors:
        return result, []
    invalid = _unresolved(
        trace,
        "INFRA_ERROR",
        attempt,
        "infrastructure",
        {
            "error": "Replay returned evidence that violates the result contract",
            "attempt": attempt,
            "contract_errors": errors,
            "invalid_result": result.to_dict(),
        },
    )
    return invalid, errors


def _flaky_inconclusive_result(result):
    """Return a detached result whose terminal evidence matches INCONCLUSIVE."""
    flaky = TraceResult.from_dict(result.to_dict())
    flaky.passed = False
    flaky.status = "INCONCLUSIVE"
    flaky.stop_reason = "FLAKY"
    # A hard terminal report has a terminal step whose status follows the
    # result. A soft-only continued replay ends on a later PASS step; changing
    # that unrelated step to INCONCLUSIVE would make the evidence invalid.
    if (flaky.steps and flaky.steps[-1].status == "FAIL"
            and not flaky.steps[-1].soft_failed):
        flaky.steps[-1].status = "INCONCLUSIVE"
    return flaky


def _reports_for_result(trace: Trace, result: TraceResult,
                        unit_page_map: dict) -> list[RepairReport]:
    """Return all observed reports while accepting legacy singular results."""
    return build_repair_reports(trace, result, unit_page_map)


def _confirmation_fingerprints(reports: list[RepairReport]) -> list[str]:
    """Ordered full evidence identity for a replay confirmation."""
    return [report.confirmation_fingerprint for report in reports]


def _save_contract_result(trace, result, path):
    errors = result.contract_errors(trace)
    if errors:
        raise ValueError(f"Refusing to persist an invalid TraceResult: {errors}")
    save_json(result.to_dict(), path)


def run_gate(req: GateRequest, harmony=None, replay_fn: Optional[ReplayFn] = None) -> GateResult:
    """Serialize one workspace and always save failures, including budget exhaustion."""
    os.makedirs(req.workspace, exist_ok=True)
    with FileLock(os.path.join(req.workspace, ".gate.lock")):
        # Conservative host lock covers explicit and implicit serial selection
        # across different workspaces; no two gates can reset the same device.
        with FileLock(os.path.join(tempfile.gettempdir(), "a2h-harmony-differential-device.lock")):
            return _run_gate(req, harmony, replay_fn)


def _run_gate(req, harmony, replay_fn):
    t0 = time.monotonic()
    history_dir = os.path.join(req.workspace, "history")
    round_no = _next_round_no(history_dir)
    runs_root = os.path.join(req.workspace, "runs", f"round_{round_no:03d}")
    reports, flaky_now, ran, results = [], [], [], []
    trace_status, trace_passed = {}, {}
    selected, skipped, traces = [], [], []
    upm, stop_reason, caught_budget = {}, "", None
    phase = "baseline"
    metadata = dict(req.deployment_metadata)
    metadata.update(bundle=req.bundle, requested_device=req.harmony_device)
    flaky_list = None
    replay_attempts = 0
    current_trace = None
    try:
        with deadline_scope(req.time_budget_s):
            flaky_list = FlakyList(os.path.join(req.workspace, "flaky.json"))
            cfg = Config.load(req.config_yaml)
            cfg.oracle.enable_l3 = False
            if req.hdc_path:
                cfg.device.hdc_path = req.hdc_path
            if req.harmony_device:
                cfg.device.harmony_serial = req.harmony_device
            traces = load_seeds(req.seeds_dir or os.path.join(req.workspace, "seeds"))
            if not traces:
                raise ValueError("No baseline traces; record Trace v2 seeds before replay")
            ids = [trace.trace_id for trace in traces]
            if len(set(ids)) != len(ids) or any(not re.fullmatch(r"[\w.-]+", tid) or tid in (".", "..") for tid in ids):
                raise ValueError("Trace IDs must be unique safe directory names")
            upm_path = os.path.join(req.workspace, "unit_page_map.json")
            upm = load_json(upm_path) if os.path.exists(upm_path) else {}
            pairs_path = os.path.join(req.workspace, "page_pairs.json")
            pairs = PagePairs.load(pairs_path if os.path.exists(pairs_path) else None,
                                   stem_sim_min=cfg.oracle.page_stem_sim_min)
            selected, skipped = select_traces(traces, req.changed_units, upm, req.full_replay)
            if req.trace_ids:
                requested = set(req.trace_ids)
                unknown = sorted(requested - set(ids))
                if unknown:
                    raise ValueError(f"Unknown requested trace IDs: {unknown}")
                selected = [trace for trace in selected if trace.trace_id in requested]
                selected_ids = {trace.trace_id for trace in selected}
                skipped = [trace for trace in traces if trace.trace_id not in selected_ids]
            print(f"[gate] round={round_no} traces={len(selected)} "
                  f"confirm_failures={req.confirm_failures}", flush=True)
            trace_status = {t.trace_id: "NOT_RUN" for t in traces}
            invalid = [(t, t.baseline_errors()) for t in selected if t.baseline_errors()]
            if invalid:
                for trace, errors in invalid:
                    result = _unresolved(trace, "BASELINE_INVALID", "baseline", "baseline", {"errors": errors})
                    results.append(result)
                    trace_status[trace.trace_id] = "INCONCLUSIVE"
                    reports.extend(_reports_for_result(trace, result, upm))
                    save_json(result.to_dict(), os.path.join(runs_root, trace.trace_id, "result.json"))
                stop_reason = "BASELINE_INVALID"
            elif not selected:
                stop_reason = "NO_TRACES_SELECTED"
            else:
                phase = "deploy"
                if harmony is None:
                    from ..adapters.harmony import HarmonyAdapter
                    harmony = HarmonyAdapter(cfg, bundle=req.bundle, serial=cfg.device.harmony_serial)
                if hasattr(harmony, "use_bounded_backend"):
                    harmony.use_bounded_backend()
                if req.hap_path:
                    digest = file_sha256(req.hap_path)
                    if digest is None:
                        raise FileNotFoundError(f"HAP does not exist: {req.hap_path}")
                    expected_digest = metadata.get("hap_sha256")
                    if expected_digest and expected_digest != digest:
                        raise ValueError("HAP digest differs from build metadata")
                    metadata.update(hap_path=os.path.abspath(req.hap_path), hap_sha256=digest)
                    harmony.install(req.hap_path)
                    metadata["installed"] = True
                harmony.ensure_ready()
                if hasattr(harmony, "deployment_identity"):
                    metadata["device_identity"] = harmony.deployment_identity()
                if replay_fn is None:
                    from ..replayer import replay as replay_fn
                phase = "replay"
                for index, trace in enumerate(selected, 1):
                    current_trace = trace
                    check_budget()
                    consume_budget("replays")
                    ran.append(trace.trace_id)
                    replay_attempts += 1
                    print(f"[gate] {index}/{len(selected)} {trace.trace_id}: replay", flush=True)
                    result, _ = _contract_checked_result(
                        trace,
                        replay_fn(trace, harmony, pairs, cfg, runs_root),
                        "replay",
                    )
                    check_budget()
                    results.append(result)
                    save_json(result.to_dict(), os.path.join(runs_root, trace.trace_id, "result.json"))
                    print(f"[gate] {trace.trace_id}: {result.status} "
                          f"executed={result.executed_steps}/{len(trace.events)} "
                          f"verified={result.verified_steps}/{len(trace.events)} "
                          f"reason={result.stop_reason or '-'}", flush=True)
                    if result.status == "PASS" and result.passed:
                        flaky_list.clear(trace.trace_id)
                        trace_status[trace.trace_id] = "PASS"
                        trace_passed[trace.trace_id] = True
                        continue
                    if result.status in ("INCONCLUSIVE", "NOT_RUN") or not (
                            result.divergences or result.divergence):
                        trace_status[trace.trace_id] = "INCONCLUSIVE"
                        reports.extend(_reports_for_result(trace, result, upm))
                        stop_reason = result.stop_reason or "REPLAY_INCONCLUSIVE"
                        break
                    if not req.confirm_failures:
                        if result.divergence:
                            result.divergence.confirmed = None
                        for divergence in result.divergences or ([result.divergence]
                                                                  if result.divergence else []):
                            divergence.confirmed = None
                        trace_status[trace.trace_id] = "FAIL"
                        trace_passed[trace.trace_id] = False
                        reports.extend(_reports_for_result(trace, result, upm))
                        save_json(result.to_dict(), os.path.join(runs_root, trace.trace_id, "result.json"))
                        continue
                    check_budget()
                    consume_budget("replays")
                    replay_attempts += 1
                    print(f"[gate] {trace.trace_id}: confirm failure", flush=True)
                    confirm, confirm_contract_errors = _contract_checked_result(
                        trace,
                        replay_fn(trace, harmony, pairs, cfg,
                                  os.path.join(runs_root, "confirm")),
                        "confirmation",
                    )
                    check_budget()
                    save_json(confirm.to_dict(), os.path.join(runs_root, "confirm", trace.trace_id, "result.json"))
                    if confirm_contract_errors:
                        results[-1] = confirm
                        trace_status[trace.trace_id] = "INCONCLUSIVE"
                        reports.extend(_reports_for_result(trace, confirm, upm))
                        stop_reason = "INFRA_ERROR"
                        save_json(confirm.to_dict(), os.path.join(runs_root, trace.trace_id, "result.json"))
                        break
                    if confirm.status in ("INCONCLUSIVE", "NOT_RUN"):
                        results[-1] = confirm
                        trace_status[trace.trace_id] = "INCONCLUSIVE"
                        reports.extend(_reports_for_result(trace, confirm, upm))
                        confirm_reports = confirm.divergences or ([confirm.divergence]
                                                                  if confirm.divergence else [])
                        stop_reason = (confirm.stop_reason
                                       or (confirm_reports[-1].kind if confirm_reports else None)
                                       or "REPLAY_INCONCLUSIVE")
                        save_json(confirm.to_dict(), os.path.join(runs_root, trace.trace_id, "result.json"))
                        break
                    initial_reports = _reports_for_result(trace, result, upm)
                    confirmed_reports = _reports_for_result(trace, confirm, upm)
                    initial_fingerprints = _confirmation_fingerprints(initial_reports)
                    replay_fingerprints = _confirmation_fingerprints(confirmed_reports)
                    reproduced = (confirm.status == "FAIL" and not confirm.passed
                                  and bool(initial_fingerprints)
                                  and replay_fingerprints == initial_fingerprints)
                    for report in initial_reports:
                        report.evidence["confirmation_fingerprints"] = {
                            "initial": initial_fingerprints,
                            "replay": replay_fingerprints,
                        }
                    if reproduced:
                        if result.divergence:
                            result.divergence.confirmed = True
                        for divergence in result.divergences or ([result.divergence]
                                                                  if result.divergence else []):
                            divergence.confirmed = True
                        for report in initial_reports:
                            report.evidence["confirmed"] = True
                        flaky_list.clear(trace.trace_id)
                        trace_status[trace.trace_id] = "FAIL"
                        trace_passed[trace.trace_id] = False
                        reports.extend(initial_reports)
                    else:
                        n = flaky_list.mark(trace.trace_id)
                        flaky_now.append(trace.trace_id)
                        trace_status[trace.trace_id] = "INCONCLUSIVE"
                        result = _flaky_inconclusive_result(result)
                        results[-1] = result
                        for report in initial_reports:
                            report.failure_type, report.cause_class = "FLAKY", "unknown"
                            report.flaky_escalated = n >= ESCALATE_AFTER
                            report.evidence["confirmation"] = confirm.to_dict()
                        reports.extend(initial_reports)
                    _save_contract_result(
                        trace, result,
                        os.path.join(runs_root, trace.trace_id, "result.json"),
                    )
    except BudgetExceeded as exc:
        caught_budget, stop_reason = exc, "BUDGET_EXHAUSTED"
        partial = getattr(exc, "trace_result", None)
        if partial and current_trace:
            partial, _ = _contract_checked_result(current_trace, partial, "budget")
            results = [r for r in results if r.trace_id != current_trace.trace_id] + [partial]
            trace_status[current_trace.trace_id] = "INCONCLUSIVE"
            reports.extend(_reports_for_result(current_trace, partial, upm))
            _save_contract_result(
                current_trace, partial,
                os.path.join(runs_root, current_trace.trace_id, "result.json"),
            )
        elif current_trace:
            trace_status[current_trace.trace_id] = "INCONCLUSIVE"
        pending = [t.trace_id for t in selected if trace_status.get(t.trace_id) not in ("PASS", "FAIL")]
        reports.append(build_timeout_report(pending, time.monotonic() - t0))
    except Exception as exc:
        kind, cause = ("BASELINE_INVALID", "baseline") if phase == "baseline" else ("INFRA_ERROR", "infrastructure")
        stop_reason = kind
        trace = current_trace or Trace("__gate__", "", req.bundle)
        result = _unresolved(trace, kind, phase, cause, {"error": str(exc), "error_type": type(exc).__name__})
        reports.extend(_reports_for_result(trace, result, upm))
        if current_trace:
            trace_status[current_trace.trace_id] = "INCONCLUSIVE"
            results.append(result)
            save_json(result.to_dict(), os.path.join(runs_root, trace.trace_id, "result.json"))
    if flaky_list is not None:
        flaky_list.save()
    chosen = [trace_status.get(t.trace_id, "NOT_RUN") for t in selected]
    status = ("PASS" if chosen and all(s == "PASS" for s in chosen) and not reports
              else "INCONCLUSIVE" if stop_reason or any(s in ("INCONCLUSIVE", "NOT_RUN") for s in chosen)
              else "FAIL" if "FAIL" in chosen else "INCONCLUSIVE")
    if not chosen and stop_reason == "NO_TRACES_SELECTED":
        status = "NOT_RUN"
    # Aggregate metrics cover every selected trace, including work that could
    # not start after an inconclusive result or budget interruption. Otherwise
    # a partial round can misleadingly retain R_replay=1 and trace_pass_rate=1.
    metric_results = list(results)
    result_ids = {result.trace_id for result in metric_results}
    metric_results.extend(
        TraceResult(
            trace.trace_id,
            len(trace.events),
            0,
            False,
            status=trace_status.get(trace.trace_id, "NOT_RUN"),
            stop_reason=("NOT_RUN" if trace_status.get(trace.trace_id, "NOT_RUN") == "NOT_RUN"
                         else stop_reason),
        )
        for trace in selected
        if trace.trace_id not in result_ids
    )
    metrics = compute_metrics(metric_results)
    present = {r["trace_id"] for r in metrics["per_trace"]}
    metrics["per_trace"].extend({"trace_id": trace.trace_id,
                                 "status": trace_status.get(trace.trace_id, "NOT_RUN"),
                                 "passed": False, "executed_steps": 0,
                                 "verified_steps": 0, "compared_steps": 0,
                                 "policy_evaluated_steps": 0,
                                 "direct_verified_steps": 0, "mediated_steps": 0,
                                 "external_recovery_failures": 0,
                                 "R_eq_direct": None, "R_eq_policy": None,
                                 "first_divergence_step": None,
                                 "kind": None, "category": None, "confirmed": None,
                                 "total_steps": len(trace.events)}
                                for trace in traces if trace.trace_id not in present)
    metrics.update(planned_traces=len(selected), attempted_traces=len(ran), replay_attempts=replay_attempts,
                   confirm_failures=req.confirm_failures,
                   not_run_traces=sum(s == "NOT_RUN" for s in chosen), flaky_traces=len(flaky_now),
                   trace_status_counts={s: chosen.count(s) for s in ("PASS", "FAIL", "INCONCLUSIVE", "NOT_RUN")})
    gate = GateResult(status == "PASS", ran, [t.trace_id for t in skipped], reports,
                      flaky_now, time.monotonic() - t0, round_no, trace_passed,
                      status, trace_status, stop_reason, metrics, metadata)
    save_json(gate.to_dict(), os.path.join(history_dir, f"round_{round_no:03d}.json"))
    print(f"[gate] round={round_no} {status} traces={len(ran)}/{len(selected)} "
          f"elapsed={gate.elapsed_s:.1f}s stop={stop_reason or '-'}", flush=True)
    logger.info("[gate] round=%d status=%s replayed=%d/%d stop=%s", round_no, status,
                len(ran), len(selected), stop_reason)
    if caught_budget is not None:
        caught_budget.gate_result = gate
        raise caught_budget
    return gate

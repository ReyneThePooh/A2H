"""Pure decision policy between differential evidence and automatic repair."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from diff_tester.gate import GateResult, RepairReport

GateAction = Literal["PASS", "VALIDATE_FULL", "REPAIR", "STOP"]
GateScope = Literal["full", "diagnostic", "full_validation"]

# Keep automatic repair eligibility closed: newly introduced or missing cause
# classes remain diagnostic until they are explicitly admitted here.
TRANSLATION_CAUSE_ALLOWLIST = frozenset({"translation"})

# Automatic source edits are permitted only for failure classes whose evidence
# has an established translation-repair path. New report types stay diagnostic
# until they are deliberately added here.
REPAIRABLE_TRANSLATION_FAILURES = frozenset({
    "ALIGN_FAIL",
    "CRASH",
    "WRONG_PAGE",
    "CONTENT_LOSS",
    "STARTUP_STATE_MISMATCH",
    "PRECONDITION_MISMATCH",
    "EXTERNAL_PROTOCOL",
})

_NON_REPAIRABLE_FAILURES = {
    "INFRA_ERROR",
    "BASELINE_INVALID",
    "BUDGET_EXHAUSTED",
    "FLAKY",
    "EMPTY_TEST_SET",
    "UNVERIFIED",
    "TIMEOUT",
    "INCONCLUSIVE",
    "NOT_RUN",
    "UNSTABLE",
    "PLATFORM_MEDIATION",
}

_CAUSE_STOP_REASONS = {
    "infrastructure": "INFRA_ERROR",
    "baseline": "BASELINE_INVALID",
    "budget": "BUDGET_EXHAUSTED",
    "unknown": "NEEDS_EVIDENCE",
    "platform": "PLATFORM_MEDIATION",
    "source": "SOURCE_BEHAVIOR",
    "source_bug": "SOURCE_BEHAVIOR",
    "noise": "FLAKY",
}


@dataclass(frozen=True)
class GateDecision:
    action: GateAction
    reason: str = ""


def is_repairable_cause(cause_class: object) -> bool:
    return (isinstance(cause_class, str)
            and cause_class.casefold() in TRANSLATION_CAUSE_ALLOWLIST)


def is_repairable_report(report: RepairReport) -> bool:
    """Return whether a confirmed report type may enter automatic repair."""
    return (is_repairable_cause(report.cause_class)
            and report.failure_type in REPAIRABLE_TRANSLATION_FAILURES)


def _report_stop_reason(report: RepairReport) -> str:
    if report.failure_type in _NON_REPAIRABLE_FAILURES:
        return report.failure_type
    cause = report.cause_class.casefold() if isinstance(report.cause_class, str) else "unknown"
    if not is_repairable_cause(report.cause_class):
        return _CAUSE_STOP_REASONS.get(cause, "NEEDS_EVIDENCE")
    if report.failure_type not in REPAIRABLE_TRANSLATION_FAILURES:
        return "NEEDS_EVIDENCE"
    return ""


def decide_gate(gate: GateResult, scope: GateScope) -> GateDecision:
    """Return the only permitted next action for one gate result.

    A report is eligible for automatic repair only after a confirmation replay
    reproduced it and classified its cause as translation. Deliberately skipped
    traces are outside a diagnostic scope, while every selected trace must have
    a decisive outcome.
    """
    if scope not in {"full", "diagnostic", "full_validation"}:
        raise ValueError(f"Unknown gate scope: {scope}")
    if gate.flaky:
        return GateDecision("STOP", "FLAKY")
    if gate.stop_reason in _NON_REPAIRABLE_FAILURES:
        return GateDecision("STOP", gate.stop_reason)
    for report in gate.reports:
        if reason := _report_stop_reason(report):
            return GateDecision("STOP", reason)
    if gate.status in _NON_REPAIRABLE_FAILURES:
        return GateDecision("STOP", gate.status)

    selected = gate.selected_traces
    statuses = gate.selected_statuses
    if not selected:
        return GateDecision("STOP", "EMPTY_TEST_SET")
    if set(selected) != set(gate.ran_traces):
        return GateDecision("STOP", "INCOMPLETE_REPLAY")
    if any(status not in {"PASS", "PASSED", "FAIL"}
           for status in statuses.values()):
        return GateDecision("STOP", "INCONCLUSIVE")
    if scope != "diagnostic" and gate.skipped_traces:
        return GateDecision("STOP", "INCOMPLETE_REPLAY")

    failed_ids = {trace_id for trace_id, status in statuses.items()
                  if status == "FAIL"}
    report_ids = {report.trace_id for report in gate.reports}
    if failed_ids != report_ids:
        return GateDecision("STOP", "NEEDS_EVIDENCE")
    if gate.passed != (not failed_ids):
        return GateDecision("STOP", "NEEDS_EVIDENCE")

    if gate.passed:
        return GateDecision("VALIDATE_FULL" if scope == "diagnostic" else "PASS")
    if any(report.evidence.get("confirmed") is not True for report in gate.reports):
        return GateDecision("STOP", "UNCONFIRMED_FAILURE")
    return GateDecision("REPAIR")

"""Summarize persisted differential-replay evidence.

This is intentionally a read-only diagnostic tool.  It understands the
canonical ``runs/**/result.json`` files written by the replay gate and the
``history/round_*.json`` reports written beside them.  It does not replay a
trace, validate a result contract, or infer a failure that is not present in
the persisted evidence.

Examples::

    python tools/summarize_replay.py .
    python tools/summarize_replay.py .diff_gate/runs --json
    python tools/summarize_replay.py .diff_gate/history/round_001.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping


_ROUND_RE = re.compile(
    r"(?:^|[\\/_-])round[_-](\d+)(?:\.json)?(?:$|[\\/_-])",
    re.IGNORECASE,
)


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _first_text(*values: Any) -> str | None:
    for value in values:
        text = _text(value)
        if text is not None:
            return text
    return None


def _predicates(*values: Any) -> tuple[str, ...]:
    """Return stable, de-duplicated failed predicate identifiers."""
    result: list[str] = []
    for value in values:
        if not isinstance(value, (list, tuple)):
            continue
        for item in value:
            if isinstance(item, str) and item and item not in result:
                result.append(item)
    return tuple(result)


def _round_id(path: Path) -> str | None:
    match = _ROUND_RE.search(path.as_posix())
    return match.group(1) if match else None


def _source_kind(path: Path) -> str:
    return "history" if path.name.startswith("round_") else "result"


def discover_inputs(paths: Iterable[str | Path]) -> tuple[list[Path], list[str]]:
    """Discover result and history files below the supplied paths.

    A file argument is accepted directly.  A ``runs`` directory searches for
    ``result.json``; a ``history`` directory searches for ``round_*.json``.
    Other directories are treated as a workspace root and may contain both.
    Returned paths are sorted and de-duplicated.
    """
    if isinstance(paths, (str, Path)):
        paths = [paths]
    found: set[Path] = set()
    errors: list[str] = []

    for raw in paths:
        path = Path(raw)
        if not path.exists():
            errors.append(f"input does not exist: {path}")
            continue
        if path.is_file():
            if path.name == "result.json" or (
                path.name.startswith("round_") and path.suffix == ".json"
            ):
                found.add(path.resolve())
            else:
                errors.append(f"unsupported input file (expected result.json or round_*.json): {path}")
            continue

        name = path.name.casefold()
        if name == "runs":
            candidates = path.rglob("result.json")
        elif name == "history":
            candidates = path.rglob("round_*.json")
        else:
            candidates = list(path.rglob("result.json")) + list(
                path.rglob("round_*.json")
            )
        for candidate in candidates:
            if candidate.is_file():
                found.add(candidate.resolve())

    return sorted(found, key=lambda item: item.as_posix()), errors


def _record(
    *,
    source: Path,
    trace_id: Any,
    kind: Any,
    failed_predicates: Iterable[str] = (),
    cause_class: Any = None,
    diverged_step: Any = None,
    baseline_invalid: bool = False,
    report_index: int | None = None,
) -> dict[str, Any] | None:
    trace = _text(trace_id)
    divergence_kind = _text(kind)
    if trace is None and divergence_kind is None and not baseline_invalid:
        return None
    if trace is None:
        trace = "<unknown>"
    if baseline_invalid and divergence_kind is None:
        divergence_kind = "BASELINE_INVALID"
    if divergence_kind is None and not failed_predicates and cause_class is None:
        return None

    step: int | None
    try:
        step = int(diverged_step) if diverged_step is not None else None
    except (TypeError, ValueError):
        step = None
    cause = _text(cause_class)
    predicates = tuple(failed_predicates)
    return {
        "source": str(source),
        "source_kind": _source_kind(source),
        "round": _round_id(source),
        "trace_id": trace,
        "divergence_kind": divergence_kind,
        "failed_predicates": list(predicates),
        "cause_class": cause,
        "diverged_step": step,
        "baseline_invalid": bool(baseline_invalid),
        "report_index": report_index,
    }


def _from_result_divergence(
    payload: Mapping[str, Any], source: Path,
    divergence: Mapping[str, Any], report_index: int | None = None,
) -> dict[str, Any] | None:
    detail = _mapping(divergence.get("detail")) or {}
    stop_reason = _text(payload.get("stop_reason"))
    kind = _first_text(divergence.get("kind"), stop_reason)
    baseline_invalid = kind == "BASELINE_INVALID" or stop_reason == "BASELINE_INVALID"
    predicates = _predicates(
        detail.get("failed_predicates"),
        divergence.get("failed_predicates"),
        payload.get("failed_predicates"),
    )
    cause = _first_text(
        divergence.get("cause_class"),
        detail.get("cause_class"),
        payload.get("cause_class"),
    )
    step = divergence.get("diverged_step", payload.get("first_divergence_step"))
    return _record(
        source=source,
        trace_id=payload.get("trace_id"),
        kind=kind,
        failed_predicates=predicates,
        cause_class=cause,
        diverged_step=step,
        baseline_invalid=baseline_invalid,
        report_index=report_index,
    )


def _from_result_records(
    payload: Mapping[str, Any], source: Path,
) -> list[dict[str, Any]]:
    persisted = payload.get("divergences")
    if isinstance(persisted, list):
        records = []
        for index, item in enumerate(persisted):
            divergence = _mapping(item)
            if divergence is None:
                continue
            record = _from_result_divergence(payload, source, divergence, index)
            if record is not None:
                records.append(record)
        if records:
            return records

    divergence = _mapping(payload.get("divergence"))
    if divergence is None:
        # A stop reason without typed divergence is still useful for baseline
        # and infrastructure diagnostics.
        divergence = {}
    record = _from_result_divergence(payload, source, divergence)
    return [record] if record is not None else []


def _from_result(payload: Mapping[str, Any], source: Path) -> dict[str, Any] | None:
    """Legacy singular helper; callers needing all evidence use records."""
    records = _from_result_records(payload, source)
    return records[0] if records else None


def _from_history_report(
    report: Mapping[str, Any], source: Path, report_index: int
) -> dict[str, Any] | None:
    evidence = _mapping(report.get("evidence")) or {}
    detail = _mapping(evidence.get("detail")) or {}
    confirmation = _mapping(evidence.get("confirmation")) or {}
    confirmation_divergence = _mapping(confirmation.get("divergence")) or {}
    confirmation_detail = _mapping(confirmation_divergence.get("detail")) or {}

    # A FLAKY history report carries the actual replay result under
    # evidence.confirmation.  Prefer that typed kind when it is available.
    kind = _first_text(
        confirmation_divergence.get("kind"),
        _mapping(evidence.get("divergence")) and _mapping(evidence["divergence"]).get("kind"),
        report.get("failure_type"),
    )
    predicates = _predicates(
        detail.get("failed_predicates"),
        confirmation_detail.get("failed_predicates"),
    )
    cause = _first_text(
        report.get("cause_class"),
        confirmation_divergence.get("cause_class"),
        detail.get("cause_class"),
    )
    step = report.get("diverged_step", confirmation_divergence.get("diverged_step"))
    baseline_invalid = (
        kind == "BASELINE_INVALID"
        or _text(report.get("failure_type")) == "BASELINE_INVALID"
        or _text(confirmation.get("stop_reason")) == "BASELINE_INVALID"
    )
    trace_id = _first_text(report.get("trace_id"), confirmation.get("trace_id"))
    return _record(
        source=source,
        trace_id=trace_id,
        kind=kind,
        failed_predicates=predicates,
        cause_class=cause,
        diverged_step=step,
        baseline_invalid=baseline_invalid,
        report_index=report_index,
    )


def _iter_file_records(path: Path) -> Iterator[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return
    data = _mapping(payload)
    if data is None:
        return

    if path.name == "result.json":
        for record in _from_result_records(data, path):
            yield record
        return

    reports = data.get("reports")
    if isinstance(reports, list):
        for index, item in enumerate(reports):
            report = _mapping(item)
            if report is None:
                continue
            record = _from_history_report(report, path, index)
            if record is not None:
                yield record
        return

    # Permit a history file containing a single result-shaped object.
    record = _from_result(data, path)
    if record is not None:
        yield record


def _record_key(record: Mapping[str, Any]) -> tuple[Any, ...]:
    """Key used to collapse runs/history copies from one gate round."""
    return (
        _evidence_scope(Path(str(record.get("source", "")))),
        record.get("round"),
        record.get("trace_id"),
        record.get("divergence_kind"),
        record.get("diverged_step"),
        tuple(record.get("failed_predicates", ())),
        record.get("cause_class"),
    )


def _evidence_scope(path: Path) -> str:
    """Return the gate workspace containing a ``runs``/``history`` path."""
    parts = path.parts
    positions = [index for index, part in enumerate(parts) if part.casefold() in {"runs", "history"}]
    if positions:
        return str(Path(*parts[: positions[0]]))
    return str(path.parent)


def summarize_paths(paths: Iterable[str | Path]) -> dict[str, Any]:
    """Return aggregate replay evidence for files below ``paths``."""
    if isinstance(paths, (str, Path)):
        paths = [paths]
    files, errors = discover_inputs(paths)
    records: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for path in files:
        before = len(records)
        for record in _iter_file_records(path):
            key = _record_key(record)
            if key in seen:
                continue
            seen.add(key)
            records.append(record)
        if len(records) == before:
            # A malformed or empty file is useful context to callers, but it is
            # not replay evidence and must not turn an empty run into a pass.
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                errors.append(f"could not parse JSON: {path}")
            else:
                if not isinstance(data, Mapping):
                    errors.append(f"JSON object expected: {path}")

    kind_counts = Counter(
        record["divergence_kind"]
        for record in records
        if record.get("divergence_kind")
    )
    predicate_counts = Counter(
        predicate
        for record in records
        for predicate in record.get("failed_predicates", ())
    )
    cause_counts = Counter(
        record["cause_class"]
        for record in records
        if record.get("cause_class")
    )
    step_counts = Counter(
        str(record["diverged_step"])
        for record in records
        if record.get("diverged_step") is not None
    )
    return {
        "evidence": bool(records),
        "files_scanned": len(files),
        "record_count": len(records),
        "baseline_invalid": sum(
            1 for record in records if record.get("baseline_invalid")
        ),
        "divergence_kinds": dict(sorted(kind_counts.items())),
        "failed_predicates": dict(sorted(predicate_counts.items())),
        "cause_classes": dict(sorted(cause_counts.items())),
        "diverged_steps": dict(sorted(step_counts.items(), key=lambda item: int(item[0]))),
        "records": records,
        "errors": errors,
    }


def _render(summary: Mapping[str, Any]) -> str:
    if not summary.get("evidence"):
        message = "无证据：未发现可解析的回放 result.json 或 history/round_*.json。"
        if summary.get("errors"):
            message += f" 扫描了 {summary.get('files_scanned', 0)} 个文件。"
        return message

    lines = [
        "Replay evidence summary",
        f"records: {summary['record_count']} (files: {summary['files_scanned']})",
        f"baseline_invalid: {summary['baseline_invalid']}",
    ]
    for title, key in (
        ("divergence kinds", "divergence_kinds"),
        ("failed predicates", "failed_predicates"),
        ("cause classes", "cause_classes"),
        ("diverged steps", "diverged_steps"),
    ):
        lines.append(f"{title}:")
        values = summary.get(key) or {}
        lines.extend(f"  {name}: {count}" for name, count in values.items())
        if not values:
            lines.append("  (none)")
    if summary.get("errors"):
        lines.append("diagnostic warnings:")
        lines.extend(f"  {error}" for error in summary["errors"])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", help="workspace, runs/history directory, or JSON file")
    parser.add_argument(
        "--input", "-i", dest="input_paths", action="append", default=[],
        help="same as a positional path; may be supplied more than once",
    )
    parser.add_argument("--json", action="store_true", dest="as_json", help="print machine-readable JSON")
    args = parser.parse_args(argv)
    paths = args.paths or args.input_paths or ["."]
    if args.paths and args.input_paths:
        paths = args.paths + args.input_paths
    summary = summarize_paths(paths)
    if args.as_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(_render(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Persistent progress evidence for the functional repair loop."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

from run_control import FileLock, atomic_json


SCHEMA_VERSION = 3
_LOCK_TIMEOUT_S = 5
_GATE_SCOPES = frozenset({"full", "diagnostic", "full_validation"})
_GATE_ACTIONS = frozenset({"PASS", "VALIDATE_FULL", "REPAIR", "STOP"})
_TRANSACTION_ID = re.compile(r"[A-Za-z0-9_-]+")


class RepairLedgerError(RuntimeError):
    """The persisted repair history cannot be trusted."""


def stable_project_identity(project_dir: str | Path) -> str:
    """Return a stable identity for one canonical project location."""
    canonical = os.path.normcase(str(Path(project_dir).resolve())).replace(
        "\\", "/"
    )
    payload = f"repair-project-v1\0{canonical}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def aggregate_failure_identity(fingerprints: Iterable[str]) -> tuple[str, list[str]]:
    """Return an order-independent identity for one complete failure set."""
    values = list(fingerprints)
    if (not values
            or any(type(value) is not str or not value for value in values)):
        raise ValueError("failure fingerprints must be nonempty strings")
    strict = sorted(values)
    payload = json.dumps(strict, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest(), strict


def compute_repair_context_digest(repair_context: dict[str, Any]) -> str:
    """Hash every repair input except the digest field itself."""
    if (not isinstance(repair_context, dict)
            or any(type(key) is not str or not key for key in repair_context)):
        raise ValueError("repair context must be an object with string keys")
    payload = {key: value for key, value in repair_context.items()
               if key != "digest"}
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("repair context must be canonical JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def summarize_diffs(diffs: dict) -> tuple[str | None, list[dict[str, Any]]]:
    """Persist useful patch evidence without duplicating complete source diffs."""
    summaries = []
    canonical = []
    for failure, diff in sorted(diffs.items(), key=lambda item: repr(item[0])):
        text = str(diff)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        added = sum(line.startswith("+") and not line.startswith("+++")
                    for line in text.splitlines())
        removed = sum(line.startswith("-") and not line.startswith("---")
                      for line in text.splitlines())
        signature = list(failure) if isinstance(failure, tuple) else str(failure)
        summaries.append({
            "failure": signature,
            "sha256": digest,
            "added_lines": added,
            "removed_lines": removed,
            "characters": len(text),
        })
        canonical.append((signature, text))
    if not canonical:
        return None, []
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), summaries


class RepairLedger:
    """Append-only logical journal stored atomically as one JSON document."""

    def __init__(self, workspace: str | Path, *, project_identity: str):
        if not self._valid_nonempty_string(project_identity):
            raise RepairLedgerError(
                "REPAIR_LEDGER_INVALID: invalid project identity"
            )
        self.project_identity = project_identity
        self.path = Path(workspace) / "repair_ledger.json"
        self.lock_path = self.path.with_name(".repair_ledger.lock")
        with FileLock(self.lock_path, timeout=_LOCK_TIMEOUT_S):
            if not self.path.exists():
                atomic_json(self.path, self._document([]))
            self.events = self._load()

    def _document(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "project_identity": self.project_identity,
            "events": events,
        }

    def _load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RepairLedgerError(
                "REPAIR_LEDGER_INVALID: unreadable repair ledger"
            ) from exc
        if (not isinstance(data, dict)
                or type(data.get("schema_version")) is not int
                or data.get("schema_version") != SCHEMA_VERSION
                or not isinstance(data.get("events"), list)
                or any(not isinstance(event, dict) for event in data["events"])):
            raise RepairLedgerError(
                "REPAIR_LEDGER_INVALID: unsupported repair ledger schema"
            )
        stored_identity = data.get("project_identity")
        if not self._valid_nonempty_string(stored_identity):
            raise RepairLedgerError(
                "REPAIR_LEDGER_INVALID: missing project identity"
            )
        if stored_identity != self.project_identity:
            raise RepairLedgerError(
                "REPAIR_LEDGER_PROJECT_MISMATCH: workspace belongs to "
                "another project"
            )
        events = list(data["events"])
        self._validate_events(events)
        return events

    @staticmethod
    def _valid_timestamp(value: object) -> bool:
        return (
            (type(value) is int and value >= 0)
            or (type(value) is float and value >= 0 and math.isfinite(value))
        )

    @staticmethod
    def _valid_nonempty_string(value: object) -> bool:
        return type(value) is str and bool(value)

    @classmethod
    def _reduce_events(
        cls,
        events: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """Validate the journal and reduce every attempt to its lifecycle state."""
        attempts: dict[str, dict[str, Any]] = {}
        candidate_ids: set[str] = set()
        transaction_ids: set[str] = set()
        expected_sequence = 1
        for event in events:
            if (not isinstance(event, dict)
                    or not cls._valid_timestamp(event.get("recorded_at_epoch_s"))):
                raise RepairLedgerError(
                    "REPAIR_LEDGER_INVALID: invalid event timestamp"
                )
            kind = event.get("event")
            if kind == "repair_attempt":
                attempt_id = event.get("attempt_id")
                outcome = event.get("outcome")
                fingerprints = event.get("failure_fingerprints")
                context = event.get("repair_context")
                identity_matches = False
                if (isinstance(fingerprints, list) and fingerprints
                        and all(cls._valid_nonempty_string(item)
                                for item in fingerprints)):
                    expected_identity, canonical = aggregate_failure_identity(
                        fingerprints)
                    identity_matches = (
                        fingerprints == canonical
                        and event.get("failure_identity") == expected_identity
                    )
                context_matches = False
                if (isinstance(context, dict)
                        and cls._valid_nonempty_string(context.get("digest"))):
                    try:
                        context_matches = (
                            context["digest"]
                            == compute_repair_context_digest(context)
                        )
                    except ValueError:
                        context_matches = False
                valid = (
                    cls._valid_nonempty_string(attempt_id)
                    and attempt_id not in attempts
                    and type(event.get("sequence")) is int
                    and event.get("sequence") == expected_sequence
                    and identity_matches
                    and context_matches
                    and cls._valid_nonempty_string(event.get("source_before"))
                    and cls._valid_nonempty_string(
                        event.get("source_after_patch"))
                    and (event.get("patch_digest") is None
                         or cls._valid_nonempty_string(
                             event.get("patch_digest")))
                    and isinstance(event.get("diff_summary"), list)
                    and isinstance(outcome, dict)
                    and cls._valid_nonempty_string(outcome.get("code"))
                )
                details = outcome.get("details")
                transaction_id = (
                    details.get("transaction_id")
                    if isinstance(details, dict) else None
                )
                transaction_valid = (
                    transaction_id is None
                    or (
                        outcome.get("code") == "PATCH_APPLIED"
                        and type(transaction_id) is str
                        and _TRANSACTION_ID.fullmatch(transaction_id) is not None
                        and transaction_id not in transaction_ids
                    )
                )
                valid = valid and transaction_valid
                if not valid:
                    raise RepairLedgerError(
                        "REPAIR_LEDGER_INVALID: invalid repair attempt event"
                    )
                is_open = outcome["code"] == "PATCH_APPLIED"
                if transaction_id is not None:
                    transaction_ids.add(transaction_id)
                if any(state["open"] for state in attempts.values()):
                    raise RepairLedgerError(
                        "REPAIR_LEDGER_INVALID: multiple open or overlapping "
                        "repair attempts"
                    )
                attempts[attempt_id] = {
                    "attempt": event,
                    "open": is_open,
                    "candidate": None,
                    "candidate_has_gate": False,
                    "provisional": False,
                    "authoritative_gate": None,
                }
                expected_sequence += 1
                continue

            if kind == "candidate_ready":
                attempt_id = event.get("attempt_id")
                candidate_id = event.get("candidate_id")
                valid = (
                    cls._valid_nonempty_string(attempt_id)
                    and cls._valid_nonempty_string(candidate_id)
                    and candidate_id not in candidate_ids
                    and cls._valid_nonempty_string(event.get("tested_source"))
                    and (event.get("build_proof_sha256") is None
                         or cls._valid_nonempty_string(
                             event.get("build_proof_sha256")))
                )
                if not valid:
                    raise RepairLedgerError(
                        "REPAIR_LEDGER_INVALID: invalid candidate event"
                    )
                state = attempts.get(attempt_id)
                if state is None or not state["open"]:
                    raise RepairLedgerError(
                        "REPAIR_LEDGER_INVALID: candidate has no open attempt"
                    )
                if state["provisional"]:
                    raise RepairLedgerError(
                        "REPAIR_LEDGER_INVALID: candidate changed after diagnostic"
                    )
                previous = state["candidate"]
                if (previous is not None
                        and previous["tested_source"] == event["tested_source"]
                        and previous.get("build_proof_sha256")
                        == event.get("build_proof_sha256")):
                    raise RepairLedgerError(
                        "REPAIR_LEDGER_INVALID: duplicate candidate event"
                    )
                candidate_ids.add(candidate_id)
                state["candidate"] = event
                state["candidate_has_gate"] = False
                continue

            if kind != "gate_result":
                raise RepairLedgerError(
                    "REPAIR_LEDGER_INVALID: unknown repair ledger event"
                )
            attempt_id = event.get("attempt_id")
            decision = event.get("decision")
            decision_valid = decision is None or (
                isinstance(decision, dict)
                and decision.get("action") in _GATE_ACTIONS
                and type(decision.get("reason")) is str
            )
            valid = (
                cls._valid_nonempty_string(attempt_id)
                and cls._valid_nonempty_string(event.get("candidate_id"))
                and event.get("scope") in _GATE_SCOPES
                and cls._valid_nonempty_string(event.get("tested_source"))
                and (event.get("build_proof_sha256") is None
                     or cls._valid_nonempty_string(
                         event.get("build_proof_sha256")))
                and isinstance(event.get("gate"), dict)
                and decision_valid
            )
            if not valid:
                raise RepairLedgerError(
                    "REPAIR_LEDGER_INVALID: invalid gate result event"
                )
            state = attempts.get(attempt_id)
            if state is None or not state["open"]:
                raise RepairLedgerError(
                    "REPAIR_LEDGER_INVALID: gate has no open attempt"
                )
            candidate = state["candidate"]
            if candidate is None:
                raise RepairLedgerError(
                    "REPAIR_LEDGER_INVALID: gate has no tested candidate"
                )
            if event["tested_source"] != candidate["tested_source"]:
                raise RepairLedgerError(
                    "REPAIR_LEDGER_INVALID: gate source does not match candidate"
                )
            if (event["candidate_id"] != candidate["candidate_id"]
                    or event.get("build_proof_sha256")
                    != candidate.get("build_proof_sha256")):
                raise RepairLedgerError(
                    "REPAIR_LEDGER_INVALID: gate proof does not match candidate"
                )
            scope = event["scope"]
            action = decision.get("action") if decision is not None else None
            state["candidate_has_gate"] = True
            if state["provisional"]:
                if decision is None:
                    continue
                if scope != "full_validation" or action not in {
                        "PASS", "REPAIR", "STOP"}:
                    raise RepairLedgerError(
                        "REPAIR_LEDGER_INVALID: diagnostic requires full validation"
                    )
                state["open"] = False
                state["authoritative_gate"] = event
                continue
            if scope == "full_validation":
                raise RepairLedgerError(
                    "REPAIR_LEDGER_INVALID: full validation lacks diagnostic"
                )
            if decision is None:
                continue
            if scope == "diagnostic":
                if action == "VALIDATE_FULL":
                    state["provisional"] = True
                elif action in {"REPAIR", "STOP"}:
                    state["open"] = False
                else:
                    raise RepairLedgerError(
                        "REPAIR_LEDGER_INVALID: invalid diagnostic decision"
                    )
                continue
            if action not in {"PASS", "REPAIR", "STOP"}:
                raise RepairLedgerError(
                    "REPAIR_LEDGER_INVALID: invalid full gate decision"
                )
            state["open"] = False
            state["authoritative_gate"] = event
        return attempts

    @classmethod
    def _validate_events(cls, events: list[dict[str, Any]]) -> None:
        cls._reduce_events(events)

    def _append(
        self,
        make_event: Callable[
            [list[dict[str, Any]]], dict[str, Any] | None
        ],
    ) -> dict[str, Any] | None:
        """Reload and append under one workspace lock to avoid lost updates."""
        with FileLock(self.lock_path, timeout=_LOCK_TIMEOUT_S):
            current = self._load()
            self.events = current
            event = make_event(current)
            if event is None:
                self.events = current
                return None
            record = dict(event)
            record.setdefault("recorded_at_epoch_s", round(time.time(), 3))
            updated = [*current, record]
            self._validate_events(updated)
            atomic_json(self.path, self._document(updated))
            self.events = updated
            return record

    def has_attempt(
        self,
        source_before: str,
        failure_identity: str,
        repair_context_digest: str,
    ) -> bool:
        with FileLock(self.lock_path, timeout=_LOCK_TIMEOUT_S):
            current = self._load()
            self.events = current
            return any(
                event.get("event") == "repair_attempt"
                and event.get("source_before") == source_before
                and event.get("failure_identity") == failure_identity
                and event.get("repair_context", {}).get("digest")
                == repair_context_digest
                for event in current
            )

    def has_transaction(self, transaction_id: str) -> bool:
        """Return whether a committed attempt records this file transaction."""
        if not self._valid_nonempty_string(transaction_id):
            raise RepairLedgerError(
                "REPAIR_LEDGER_INVALID: invalid transaction identity"
            )
        with FileLock(self.lock_path, timeout=_LOCK_TIMEOUT_S):
            current = self._load()
            self.events = current
            for event in current:
                if event.get("event") != "repair_attempt":
                    continue
                details = event.get("outcome", {}).get("details")
                if (isinstance(details, dict)
                        and event.get("outcome", {}).get("code") == "PATCH_APPLIED"
                        and details.get("transaction_id") == transaction_id):
                    return True
            return False

    def open_attempt(self) -> dict[str, Any] | None:
        state = self.open_attempt_state()
        return state["attempt"] if state is not None else None

    def open_attempt_state(self) -> dict[str, Any] | None:
        """Return a detached snapshot of the unique resumable attempt."""
        with FileLock(self.lock_path, timeout=_LOCK_TIMEOUT_S):
            current = self._load()
            self.events = current
            states = self._reduce_events(current)
            opened = [state for state in states.values() if state["open"]]
            if len(opened) > 1:
                raise RepairLedgerError(
                    "REPAIR_LEDGER_INVALID: multiple open repair attempts"
                )
            if not opened:
                return None
            state = opened[0]
            return copy.deepcopy({
                "attempt": state["attempt"],
                "candidate": state["candidate"],
                "provisional": state["provisional"],
                "candidate_has_gate": state["candidate_has_gate"],
            })

    def authoritative_gate(self, attempt_id: str) -> dict[str, Any] | None:
        with FileLock(self.lock_path, timeout=_LOCK_TIMEOUT_S):
            current = self._load()
            self.events = current
            state = self._reduce_events(current).get(attempt_id)
            if state is None or state["authoritative_gate"] is None:
                return None
            return copy.deepcopy(state["authoritative_gate"])

    def append_attempt(
        self,
        *,
        source_before: str,
        source_after_patch: str,
        failure_identity: str,
        failure_fingerprints: list[str],
        repair_context: dict[str, Any],
        outcome: dict[str, Any],
        diffs: dict,
    ) -> dict[str, Any]:
        try:
            expected_identity, canonical_fingerprints = (
                aggregate_failure_identity(failure_fingerprints)
            )
        except (TypeError, ValueError) as exc:
            raise RepairLedgerError(
                "REPAIR_LEDGER_INVALID: invalid failure fingerprints"
            ) from exc
        if failure_identity != expected_identity:
            raise RepairLedgerError(
                "REPAIR_LEDGER_INVALID: inconsistent failure identity"
            )
        if not isinstance(repair_context, dict):
            raise RepairLedgerError(
                "REPAIR_LEDGER_INVALID: invalid repair context"
            )
        context = copy.deepcopy(repair_context)
        try:
            expected_context_digest = compute_repair_context_digest(context)
        except ValueError as exc:
            raise RepairLedgerError(
                "REPAIR_LEDGER_INVALID: invalid repair context"
            ) from exc
        if (not self._valid_nonempty_string(context.get("digest"))
                or context["digest"] != expected_context_digest):
            raise RepairLedgerError(
                "REPAIR_LEDGER_INVALID: inconsistent repair context"
            )
        if not isinstance(outcome, dict) or not isinstance(diffs, dict):
            raise RepairLedgerError(
                "REPAIR_LEDGER_INVALID: invalid repair outcome"
            )
        outcome_record = copy.deepcopy(outcome)
        patch_digest, diff_summary = summarize_diffs(diffs)

        def make_event(current):
            states = self._reduce_events(current)
            if any(state["open"] for state in states.values()):
                raise RepairLedgerError(
                    "REPAIR_LEDGER_INVALID: open repair attempt already exists"
                )
            sequence = 1 + sum(
                event.get("event") == "repair_attempt" for event in current
            )
            return {
                "event": "repair_attempt",
                "attempt_id": uuid.uuid4().hex,
                "sequence": sequence,
                "failure_identity": expected_identity,
                "failure_fingerprints": canonical_fingerprints,
                "source_before": source_before,
                "source_after_patch": source_after_patch,
                "repair_context": context,
                "patch_digest": patch_digest,
                "diff_summary": diff_summary,
                "outcome": outcome_record,
            }

        record = self._append(make_event)
        assert record is not None
        return record

    def append_candidate_ready(
        self,
        *,
        attempt_id: str,
        tested_source: str,
        build_proof_sha256: str | None = None,
    ) -> dict[str, Any]:
        existing: dict[str, Any] | None = None

        def make_event(current):
            nonlocal existing
            state = self._reduce_events(current).get(attempt_id)
            if state is None or not state["open"]:
                raise RepairLedgerError(
                    "REPAIR_LEDGER_INVALID: candidate has no open attempt"
                )
            candidate = state["candidate"]
            if (candidate is not None
                    and candidate["tested_source"] == tested_source
                    and candidate.get("build_proof_sha256")
                    == build_proof_sha256):
                existing = candidate
                return None
            if state["provisional"]:
                raise RepairLedgerError(
                    "REPAIR_LEDGER_INVALID: candidate changed after diagnostic"
                )
            return {
                "event": "candidate_ready",
                "candidate_id": uuid.uuid4().hex,
                "attempt_id": attempt_id,
                "tested_source": tested_source,
                "build_proof_sha256": build_proof_sha256,
            }

        record = self._append(make_event)
        if record is not None:
            return record
        assert existing is not None
        return copy.deepcopy(existing)

    def append_gate_result(
        self,
        *,
        attempt_id: str,
        scope: str,
        tested_source: str,
        build_proof_sha256: str | None,
        gate_summary: dict[str, Any],
        decision: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(gate_summary, dict):
            raise RepairLedgerError(
                "REPAIR_LEDGER_INVALID: invalid gate summary"
            )
        if decision is not None and not isinstance(decision, dict):
            raise RepairLedgerError(
                "REPAIR_LEDGER_INVALID: invalid gate decision"
            )
        gate_record = copy.deepcopy(gate_summary)
        decision_record = copy.deepcopy(decision)

        def make_event(current):
            state = self._reduce_events(current).get(attempt_id)
            if state is None or not state["open"]:
                raise RepairLedgerError(
                    "REPAIR_LEDGER_INVALID: gate has no open attempt"
                )
            candidate = state["candidate"]
            if candidate is None:
                raise RepairLedgerError(
                    "REPAIR_LEDGER_INVALID: gate has no tested candidate"
                )
            if candidate["tested_source"] != tested_source:
                raise RepairLedgerError(
                    "REPAIR_LEDGER_INVALID: gate source does not match candidate"
                )
            if candidate.get("build_proof_sha256") != build_proof_sha256:
                raise RepairLedgerError(
                    "REPAIR_LEDGER_INVALID: gate proof does not match candidate"
                )
            return {
                "event": "gate_result",
                "attempt_id": attempt_id,
                "candidate_id": candidate["candidate_id"],
                "scope": scope,
                "tested_source": tested_source,
                "build_proof_sha256": build_proof_sha256,
                "decision": decision_record,
                "gate": gate_record,
            }

        record = self._append(make_event)
        assert record is not None
        return record

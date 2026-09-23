"""Schema-v3 lifecycle and integrity regressions for the repair ledger."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json

import pytest

from pipeline.repair_ledger import (
    RepairLedger as _RepairLedger,
    RepairLedgerError,
    SCHEMA_VERSION,
    aggregate_failure_identity,
    compute_repair_context_digest,
)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


PROJECT_ID = digest("project-one")


def RepairLedger(workspace, *, project_identity=PROJECT_ID):
    return _RepairLedger(workspace, project_identity=project_identity)


def repair_context(label="base", **overrides):
    context = {
        "schema_version": 1,
        "manifest_sha256": digest(f"manifest-{label}"),
        "plan_sha256": digest(f"plan-{label}"),
        "policy_version": 1,
        "policy_sha256": digest(f"policy-{label}"),
        "system_prompt_sha256": digest(f"prompt-{label}"),
        "model_id": f"model-{label}",
        **overrides,
    }
    context["digest"] = compute_repair_context_digest(context)
    return context


def failure_identity(label="failure"):
    fingerprints = [digest(label)]
    identity, canonical = aggregate_failure_identity(fingerprints)
    return identity, canonical


def append_attempt(ledger, label="one", *, code="PATCH_APPLIED", context=None):
    identity, fingerprints = failure_identity(f"failure-{label}")
    return ledger.append_attempt(
        source_before=digest(f"before-{label}"),
        source_after_patch=digest(f"patch-{label}"),
        failure_identity=identity,
        failure_fingerprints=fingerprints,
        repair_context=context or repair_context(),
        outcome={"code": code},
        diffs=({label: f"diff-{label}"}
               if code == "PATCH_APPLIED" else {}),
    )


def append_candidate(ledger, attempt, source="built", proof="proof"):
    return ledger.append_candidate_ready(
        attempt_id=attempt["attempt_id"],
        tested_source=digest(source),
        build_proof_sha256=digest(proof),
    )


def gate_decision(action, reason="test"):
    return {"action": action, "reason": reason}


def append_gate(ledger, attempt, source, scope, action=None, proof="proof"):
    return ledger.append_gate_result(
        attempt_id=attempt["attempt_id"],
        scope=scope,
        tested_source=digest(source),
        build_proof_sha256=digest(proof),
        gate_summary={"passed": action == "PASS"},
        decision=None if action is None else gate_decision(action),
    )


def raw_attempt(sequence=1, label="one", *, code="PATCH_APPLIED", context=None):
    identity, fingerprints = failure_identity(f"failure-{label}")
    return {
        "event": "repair_attempt",
        "attempt_id": digest(f"attempt-{label}")[:32],
        "sequence": sequence,
        "failure_identity": identity,
        "failure_fingerprints": fingerprints,
        "repair_context": context or repair_context(),
        "source_before": digest(f"before-{label}"),
        "source_after_patch": digest(f"patch-{label}"),
        "patch_digest": digest(f"diff-{label}") if code == "PATCH_APPLIED" else None,
        "diff_summary": [],
        "outcome": {"code": code},
        "recorded_at_epoch_s": 1.0,
    }


def raw_candidate(attempt, source="built"):
    return {
        "event": "candidate_ready",
        "candidate_id": digest(f"candidate-{attempt['attempt_id']}")[:32],
        "attempt_id": attempt["attempt_id"],
        "tested_source": digest(source),
        "build_proof_sha256": digest("proof"),
        "recorded_at_epoch_s": 2.0,
    }


def raw_gate(attempt, source="built", scope="full", action="PASS"):
    return {
        "event": "gate_result",
        "attempt_id": attempt["attempt_id"],
        "candidate_id": digest(f"candidate-{attempt['attempt_id']}")[:32],
        "scope": scope,
        "tested_source": digest(source),
        "build_proof_sha256": digest("proof"),
        "decision": None if action is None else gate_decision(action),
        "gate": {"passed": action == "PASS"},
        "recorded_at_epoch_s": 3.0,
    }


def write_ledger(workspace, events, schema_version=SCHEMA_VERSION,
                 project_identity=PROJECT_ID):
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "repair_ledger.json").write_text(
        json.dumps({
            "schema_version": schema_version,
            "project_identity": project_identity,
            "events": events,
        }),
        encoding="utf-8",
    )


@pytest.mark.parametrize("final_action", ["PASS", "REPAIR"])
def test_diagnostic_result_is_provisional_until_full_validation(
        tmp_path, final_action):
    ledger = RepairLedger(tmp_path)
    attempt = append_attempt(ledger)
    candidate = append_candidate(ledger, attempt, source="after-build-fix")

    diagnostic = append_gate(
        ledger, attempt, "after-build-fix", "diagnostic", "VALIDATE_FULL"
    )

    assert ledger.open_attempt()["attempt_id"] == attempt["attempt_id"]
    assert ledger.authoritative_gate(attempt["attempt_id"]) is None
    final = append_gate(
        ledger, attempt, "after-build-fix", "full_validation", final_action
    )
    assert candidate["tested_source"] != attempt["source_after_patch"]
    assert diagnostic["decision"]["action"] == "VALIDATE_FULL"
    assert ledger.authoritative_gate(attempt["attempt_id"]) == final
    assert ledger.open_attempt() is None


@pytest.mark.parametrize("action", ["REPAIR", "STOP"])
def test_diagnostic_repair_or_stop_terminates_attempt(tmp_path, action):
    ledger = RepairLedger(tmp_path)
    attempt = append_attempt(ledger)
    append_candidate(ledger, attempt)

    append_gate(ledger, attempt, "built", "diagnostic", action)

    assert ledger.open_attempt() is None
    assert ledger.authoritative_gate(attempt["attempt_id"]) is None
    replacement = append_attempt(ledger, "replacement")
    assert ledger.open_attempt()["attempt_id"] == replacement["attempt_id"]


def test_gate_source_must_match_latest_candidate_without_appending(tmp_path):
    ledger = RepairLedger(tmp_path)
    attempt = append_attempt(ledger)
    append_candidate(ledger, attempt, source="candidate")
    count = len(ledger.events)

    with pytest.raises(RepairLedgerError, match="source does not match"):
        append_gate(ledger, attempt, "different", "full", "PASS")

    assert len(RepairLedger(tmp_path).events) == count


def test_gate_build_proof_must_match_latest_candidate(tmp_path):
    ledger = RepairLedger(tmp_path)
    attempt = append_attempt(ledger)
    append_candidate(ledger, attempt, proof="current-proof")
    count = len(ledger.events)

    with pytest.raises(RepairLedgerError, match="proof does not match"):
        ledger.append_gate_result(
            attempt_id=attempt["attempt_id"],
            scope="full",
            tested_source=digest("built"),
            build_proof_sha256=digest("stale-proof"),
            gate_summary={"passed": True},
            decision=gate_decision("PASS"),
        )

    assert len(RepairLedger(tmp_path).events) == count


def test_latest_candidate_replaces_untested_build(tmp_path):
    ledger = RepairLedger(tmp_path)
    attempt = append_attempt(ledger)
    append_candidate(ledger, attempt, source="first-build")
    append_candidate(ledger, attempt, source="second-build")

    with pytest.raises(RepairLedgerError, match="source does not match"):
        append_gate(ledger, attempt, "first-build", "full", "PASS")
    append_gate(ledger, attempt, "second-build", "full", "PASS")

    assert ledger.open_attempt() is None


def test_same_candidate_is_idempotent(tmp_path):
    ledger = RepairLedger(tmp_path)
    attempt = append_attempt(ledger)
    first = append_candidate(ledger, attempt)

    repeated = append_candidate(ledger, attempt)

    assert repeated == first
    assert [event["event"] for event in ledger.events] == [
        "repair_attempt", "candidate_ready"
    ]


def test_same_source_with_new_build_proof_creates_linked_candidate(tmp_path):
    ledger = RepairLedger(tmp_path)
    attempt = append_attempt(ledger)
    first = append_candidate(ledger, attempt, proof="first-proof")

    second = append_candidate(ledger, attempt, proof="second-proof")
    gate = append_gate(
        ledger, attempt, "built", "full", "PASS", proof="second-proof"
    )

    assert second["candidate_id"] != first["candidate_id"]
    assert second["tested_source"] == first["tested_source"]
    assert gate["candidate_id"] == second["candidate_id"]
    assert gate["build_proof_sha256"] == second["build_proof_sha256"]


def test_build_failure_can_resume_same_open_attempt(tmp_path):
    first_run = RepairLedger(tmp_path)
    attempt = append_attempt(first_run)
    assert first_run.open_attempt()["attempt_id"] == attempt["attempt_id"]

    resumed = RepairLedger(tmp_path)
    open_attempt = resumed.open_attempt()
    append_candidate(resumed, open_attempt, source="resume-build")
    append_gate(resumed, open_attempt, "resume-build", "full", "PASS")

    assert resumed.open_attempt() is None


def test_partial_gate_remains_open_and_allows_rebuilt_candidate(tmp_path):
    ledger = RepairLedger(tmp_path)
    attempt = append_attempt(ledger)
    append_candidate(ledger, attempt, source="first")
    append_gate(ledger, attempt, "first", "full", action=None)

    assert ledger.open_attempt()["attempt_id"] == attempt["attempt_id"]
    append_candidate(ledger, attempt, source="rebuilt")
    append_gate(ledger, attempt, "rebuilt", "full", "PASS")

    assert ledger.open_attempt() is None


def test_partial_full_validation_does_not_close_provisional_attempt(tmp_path):
    ledger = RepairLedger(tmp_path)
    attempt = append_attempt(ledger)
    append_candidate(ledger, attempt)
    append_gate(ledger, attempt, "built", "diagnostic", "VALIDATE_FULL")
    append_gate(ledger, attempt, "built", "full_validation", action=None)

    assert ledger.open_attempt()["attempt_id"] == attempt["attempt_id"]
    append_gate(ledger, attempt, "built", "full_validation", "PASS")
    assert ledger.open_attempt() is None


def test_open_attempt_state_is_detached_and_preserves_resume_phase(tmp_path):
    ledger = RepairLedger(tmp_path)
    attempt = append_attempt(ledger)
    candidate = append_candidate(ledger, attempt)
    append_gate(ledger, attempt, "built", "diagnostic", "VALIDATE_FULL")

    state = ledger.open_attempt_state()

    assert state == {
        "attempt": attempt,
        "candidate": candidate,
        "provisional": True,
        "candidate_has_gate": True,
    }
    state["attempt"]["attempt_id"] = "mutated"
    state["candidate"]["tested_source"] = "mutated"
    refreshed = ledger.open_attempt_state()
    assert refreshed["attempt"]["attempt_id"] == attempt["attempt_id"]
    assert refreshed["candidate"]["tested_source"] == candidate["tested_source"]


def test_non_patch_outcome_never_opens_or_absorbs_gate(tmp_path):
    ledger = RepairLedger(tmp_path)
    attempt = append_attempt(ledger, code="MODEL_NO_PROPOSAL")

    assert ledger.open_attempt() is None
    with pytest.raises(RepairLedgerError, match="candidate has no open attempt"):
        append_candidate(ledger, attempt)
    with pytest.raises(RepairLedgerError, match="gate has no open attempt"):
        append_gate(ledger, attempt, "built", "full", "PASS")


def test_any_new_attempt_is_rejected_while_an_attempt_is_open(tmp_path):
    ledger = RepairLedger(tmp_path)
    append_attempt(ledger, "open")
    count = len(ledger.events)

    with pytest.raises(RepairLedgerError, match="open repair attempt"):
        append_attempt(ledger, "terminal", code="MODEL_NO_PROPOSAL")

    assert len(RepairLedger(tmp_path).events) == count


@pytest.mark.parametrize("field,value", [
    ("manifest_sha256", "changed-manifest"),
    ("plan_sha256", "changed-plan"),
    ("policy_sha256", "changed-policy"),
    ("system_prompt_sha256", "changed-prompt"),
    ("model_id", "changed-model"),
])
def test_no_progress_identity_includes_repair_context(
        tmp_path, field, value):
    ledger = RepairLedger(tmp_path)
    context = repair_context()
    attempt = append_attempt(
        ledger, code="MODEL_NO_PROPOSAL", context=context
    )
    changed = repair_context(**{field: value})

    assert ledger.has_attempt(
        attempt["source_before"], attempt["failure_identity"], context["digest"]
    )
    assert not ledger.has_attempt(
        attempt["source_before"], attempt["failure_identity"], changed["digest"]
    )


def test_stale_instances_reload_before_allocating_and_appending(tmp_path):
    first = RepairLedger(tmp_path)
    stale = RepairLedger(tmp_path)

    one = append_attempt(first, "one", code="MODEL_NO_PROPOSAL")
    two = append_attempt(stale, "two", code="MODEL_NO_PROPOSAL")

    events = RepairLedger(tmp_path).events
    assert [event["sequence"] for event in events] == [1, 2]
    assert [event["attempt_id"] for event in events] == [
        one["attempt_id"], two["attempt_id"]
    ]


def test_concurrent_terminal_attempts_are_not_lost(tmp_path):
    ledgers = [RepairLedger(tmp_path) for _ in range(12)]

    with ThreadPoolExecutor(max_workers=6) as pool:
        attempts = list(pool.map(
            lambda pair: append_attempt(
                pair[1], str(pair[0]), code="MODEL_NO_PROPOSAL"
            ),
            enumerate(ledgers),
        ))

    events = RepairLedger(tmp_path).events
    assert len(events) == len(attempts)
    assert [event["sequence"] for event in events] == list(range(1, 13))


def test_has_attempt_refreshes_a_stale_reader(tmp_path):
    reader = RepairLedger(tmp_path)
    writer = RepairLedger(tmp_path)
    context = repair_context()
    attempt = append_attempt(
        writer, "new", code="MODEL_NO_PROPOSAL", context=context
    )

    assert reader.has_attempt(
        attempt["source_before"], attempt["failure_identity"], context["digest"]
    )
    assert reader.events == writer.events


def test_ledger_binds_workspace_to_one_project_under_lock(tmp_path):
    first = RepairLedger(tmp_path, project_identity=digest("project-a"))
    same = RepairLedger(tmp_path, project_identity=digest("project-a"))

    assert first.events == same.events == []
    saved = json.loads(first.path.read_text(encoding="utf-8"))
    assert saved["project_identity"] == digest("project-a")
    with pytest.raises(RepairLedgerError, match="PROJECT_MISMATCH"):
        RepairLedger(tmp_path, project_identity=digest("project-b"))
    assert json.loads(first.path.read_text(encoding="utf-8")) == saved


def test_concurrent_first_open_cannot_bind_two_projects(tmp_path):
    identities = [digest("project-a"), digest("project-b")]

    def open_for(identity):
        try:
            return RepairLedger(tmp_path, project_identity=identity).project_identity
        except RepairLedgerError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(open_for, identities))

    saved = json.loads(
        (tmp_path / "repair_ledger.json").read_text(encoding="utf-8")
    )
    assert results.count(saved["project_identity"]) == 1
    assert sum("PROJECT_MISMATCH" in result for result in results) == 1


def test_has_transaction_refreshes_and_reads_committed_outcome(tmp_path):
    reader = RepairLedger(tmp_path)
    writer = RepairLedger(tmp_path)
    identity, fingerprints = failure_identity("transaction")
    writer.append_attempt(
        source_before=digest("before-transaction"),
        source_after_patch=digest("after-transaction"),
        failure_identity=identity,
        failure_fingerprints=fingerprints,
        repair_context=repair_context(),
        outcome={
            "code": "PATCH_APPLIED",
            "details": {"transaction_id": "txn-123"},
        },
        diffs={"transaction": "diff"},
    )

    assert reader.has_transaction("txn-123")
    assert not reader.has_transaction("txn-other")


def test_terminal_attempt_cannot_authorize_file_transaction(tmp_path):
    ledger = RepairLedger(tmp_path)
    identity, fingerprints = failure_identity("terminal-transaction")

    with pytest.raises(RepairLedgerError, match="invalid repair attempt"):
        ledger.append_attempt(
            source_before=digest("before-terminal"),
            source_after_patch=digest("after-terminal"),
            failure_identity=identity,
            failure_fingerprints=fingerprints,
            repair_context=repair_context(),
            outcome={
                "code": "MODEL_NO_PROPOSAL",
                "details": {"transaction_id": "txn-terminal"},
            },
            diffs={},
        )


def test_append_rejects_identity_unrelated_to_fingerprints(tmp_path):
    ledger = RepairLedger(tmp_path)
    _identity, fingerprints = failure_identity()

    with pytest.raises(RepairLedgerError, match="inconsistent failure identity"):
        ledger.append_attempt(
            source_before=digest("before"),
            source_after_patch=digest("after"),
            failure_identity=digest("unrelated identity"),
            failure_fingerprints=fingerprints,
            repair_context=repair_context(),
            outcome={"code": "MODEL_NO_PROPOSAL"},
            diffs={},
        )

    assert json.loads(ledger.path.read_text(encoding="utf-8"))["events"] == []


def test_load_rejects_corrupt_failure_or_context_identity(tmp_path):
    event = raw_attempt()
    event["failure_identity"] = digest("unrelated identity")
    write_ledger(tmp_path, [event])
    with pytest.raises(RepairLedgerError, match="invalid repair attempt"):
        RepairLedger(tmp_path)

    event = raw_attempt()
    event["repair_context"]["model_id"] = "changed-without-new-digest"
    write_ledger(tmp_path, [event])
    with pytest.raises(RepairLedgerError, match="invalid repair attempt"):
        RepairLedger(tmp_path)


def test_append_canonicalizes_failure_fingerprint_order(tmp_path):
    ledger = RepairLedger(tmp_path)
    fingerprints = [digest("z"), digest("a")]
    identity, canonical = aggregate_failure_identity(fingerprints)

    event = ledger.append_attempt(
        source_before=digest("before"),
        source_after_patch=digest("after"),
        failure_identity=identity,
        failure_fingerprints=fingerprints,
        repair_context=repair_context(),
        outcome={"code": "MODEL_NO_PROPOSAL"},
        diffs={},
    )

    assert event["failure_fingerprints"] == canonical


@pytest.mark.parametrize("invalid_sequence", [True, 1.0])
def test_load_rejects_non_integer_sequence_types(tmp_path, invalid_sequence):
    event = raw_attempt(sequence=invalid_sequence)
    write_ledger(tmp_path, [event])

    with pytest.raises(RepairLedgerError, match="invalid repair attempt"):
        RepairLedger(tmp_path)


@pytest.mark.parametrize("invalid_timestamp", [True, -1, float("nan")])
def test_load_rejects_invalid_timestamps(tmp_path, invalid_timestamp):
    event = raw_attempt()
    event["recorded_at_epoch_s"] = invalid_timestamp
    write_ledger(tmp_path, [event])

    with pytest.raises(RepairLedgerError, match="invalid event timestamp"):
        RepairLedger(tmp_path)


def test_multiple_open_attempts_fail_closed(tmp_path):
    write_ledger(tmp_path, [
        raw_attempt(sequence=1, label="one"),
        raw_attempt(sequence=2, label="two"),
    ])

    with pytest.raises(RepairLedgerError, match="multiple open"):
        RepairLedger(tmp_path)


def test_terminal_attempt_cannot_overlap_an_open_attempt_on_disk(tmp_path):
    write_ledger(tmp_path, [
        raw_attempt(sequence=1, label="open"),
        raw_attempt(
            sequence=2, label="terminal", code="MODEL_NO_PROPOSAL"
        ),
    ])

    with pytest.raises(RepairLedgerError, match="overlapping"):
        RepairLedger(tmp_path)


def test_gate_without_candidate_fails_closed(tmp_path):
    attempt = raw_attempt()
    write_ledger(tmp_path, [attempt, raw_gate(attempt)])

    with pytest.raises(RepairLedgerError, match="no tested candidate"):
        RepairLedger(tmp_path)


def test_full_validation_requires_provisional_diagnostic(tmp_path):
    ledger = RepairLedger(tmp_path)
    attempt = append_attempt(ledger)
    append_candidate(ledger, attempt)

    with pytest.raises(RepairLedgerError, match="lacks diagnostic"):
        append_gate(ledger, attempt, "built", "full_validation", "PASS")


def test_provisional_diagnostic_forbids_candidate_change(tmp_path):
    ledger = RepairLedger(tmp_path)
    attempt = append_attempt(ledger)
    append_candidate(ledger, attempt)
    append_gate(ledger, attempt, "built", "diagnostic", "VALIDATE_FULL")

    with pytest.raises(RepairLedgerError, match="changed after diagnostic"):
        append_candidate(ledger, attempt, source="other-build")


def test_terminal_attempt_rejects_duplicate_gate(tmp_path):
    ledger = RepairLedger(tmp_path)
    attempt = append_attempt(ledger)
    append_candidate(ledger, attempt)
    append_gate(ledger, attempt, "built", "full", "PASS")

    with pytest.raises(RepairLedgerError, match="gate has no open attempt"):
        append_gate(ledger, attempt, "built", "full", "PASS")


def test_schema_v1_is_not_silently_migrated(tmp_path):
    write_ledger(tmp_path, [], schema_version=1)

    with pytest.raises(RepairLedgerError, match="unsupported repair ledger schema"):
        RepairLedger(tmp_path)

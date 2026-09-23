"""Crash-recovery tests for pipeline-owned multi-file writes."""
from pathlib import Path

import pytest

from pipeline.file_transaction import (
    FileBatchTransaction,
    FileTransactionError,
    recover_file_transactions,
)
from run_control import atomic_write, file_sha256


def _transaction(tmp_path: Path):
    root = tmp_path / "project"
    first = root / "first.ets"
    second = root / "second.ets"
    first.parent.mkdir(parents=True)
    first.write_bytes(b"first-before")
    second.write_bytes(b"second-before")
    journal_dir = tmp_path / "work" / "transactions"
    transaction = FileBatchTransaction(
        journal_dir=journal_dir,
        owner="test_owner",
        roots=[root],
        writes={first: b"first-after", second: b"second-after"},
    )
    return root, first, second, journal_dir, transaction


def test_prepared_partial_transaction_is_rolled_back_on_recovery(tmp_path):
    root, first, second, journal_dir, transaction = _transaction(tmp_path)
    transaction.prepare()
    atomic_write(first, b"first-after")

    actions = recover_file_transactions(
        journal_dir,
        owner="test_owner",
        roots=[root],
        keep_committed=lambda _transaction_id: False,
    )

    assert actions == [(transaction.transaction_id, "rolled_back")]
    assert first.read_bytes() == b"first-before"
    assert second.read_bytes() == b"second-before"
    assert not journal_dir.exists()


def test_committed_transaction_is_kept_when_external_record_exists(tmp_path):
    root, first, second, journal_dir, transaction = _transaction(tmp_path)
    transaction.commit()

    actions = recover_file_transactions(
        journal_dir,
        owner="test_owner",
        roots=[root],
        keep_committed=lambda transaction_id: (
            transaction_id == transaction.transaction_id
        ),
    )

    assert actions == [(transaction.transaction_id, "kept")]
    assert first.read_bytes() == b"first-after"
    assert second.read_bytes() == b"second-after"
    assert not journal_dir.exists()


def test_unrecorded_committed_transaction_is_rolled_back(tmp_path):
    root, first, second, journal_dir, transaction = _transaction(tmp_path)
    transaction.commit()

    actions = recover_file_transactions(
        journal_dir,
        owner="test_owner",
        roots=[root],
        keep_committed=lambda _transaction_id: False,
    )

    assert actions == [(transaction.transaction_id, "rolled_back")]
    assert first.read_bytes() == b"first-before"
    assert second.read_bytes() == b"second-before"


def test_recovery_refuses_to_overwrite_unrelated_file_drift(tmp_path):
    root, first, _second, journal_dir, transaction = _transaction(tmp_path)
    transaction.prepare()
    first.write_bytes(b"outside-change")

    with pytest.raises(FileTransactionError, match="FILE_TRANSACTION_DRIFT"):
        recover_file_transactions(
            journal_dir,
            owner="test_owner",
            roots=[root],
            keep_committed=lambda _transaction_id: False,
        )

    assert first.read_bytes() == b"outside-change"
    assert transaction.journal_path.is_file()


def test_commit_rejects_changed_read_precondition(tmp_path):
    root, first, second, journal_dir, _unused = _transaction(tmp_path)
    observed = root / "observed.ets"
    observed.write_bytes(b"observed-before")
    transaction = FileBatchTransaction(
        journal_dir=journal_dir,
        owner="test_owner",
        roots=[root],
        writes={first: b"first-after"},
        preconditions={observed: file_sha256(observed)},
    )
    observed.write_bytes(b"outside-change")

    with pytest.raises(FileTransactionError, match="FILE_TRANSACTION_DRIFT"):
        transaction.commit()

    assert first.read_bytes() == b"first-before"
    assert second.read_bytes() == b"second-before"
    assert observed.read_bytes() == b"outside-change"
    assert not journal_dir.exists()


def test_committed_recovery_rejects_changed_read_precondition(tmp_path):
    root, first, _second, journal_dir, _unused = _transaction(tmp_path)
    observed = root / "observed.ets"
    observed.write_bytes(b"observed-before")
    transaction = FileBatchTransaction(
        journal_dir=journal_dir,
        owner="test_owner",
        roots=[root],
        writes={first: b"first-after"},
        preconditions={observed: file_sha256(observed)},
    )
    transaction.commit()
    observed.write_bytes(b"outside-change")

    with pytest.raises(
        FileTransactionError, match="FILE_TRANSACTION_PRECONDITION_DRIFT"
    ):
        recover_file_transactions(
            journal_dir,
            owner="test_owner",
            roots=[root],
            keep_committed=lambda _transaction_id: True,
        )

    assert first.read_bytes() == b"first-after"
    assert transaction.journal_path.is_file()


def test_transaction_identity_cannot_escape_journal_directory(tmp_path):
    root = tmp_path / "project"
    target = root / "source.ets"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"before")

    with pytest.raises(ValueError, match="invalid transaction identity"):
        FileBatchTransaction(
            journal_dir=tmp_path / "journals",
            owner="test_owner",
            roots=[root],
            writes={target: b"after"},
            transaction_id="../escape",
        )

"""Crash-recoverable atomic batches for pipeline-owned file updates."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping

from run_control import atomic_write


SCHEMA_VERSION = 1
_TRANSACTION_ID = re.compile(r"[A-Za-z0-9_-]+")


class FileTransactionError(RuntimeError):
    """A pending file transaction is corrupt or cannot be recovered safely."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _root_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def _owned_atomic_write(path: Path, data: bytes, transaction_id: str) -> None:
    """Atomically replace one file with a transaction-owned temp name."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".run-tx-{transaction_id}-", dir=path.parent
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _remove_journal(path: Path) -> None:
    path.unlink(missing_ok=True)
    try:
        path.parent.rmdir()
    except OSError:
        pass


class FileBatchTransaction:
    """Write several files with a durable before-image recovery journal."""

    def __init__(
        self,
        *,
        journal_dir: str | Path | None,
        owner: str,
        roots: list[str | Path],
        writes: Mapping[Path, bytes],
        preconditions: Mapping[Path, str | None] | None = None,
        transaction_id: str | None = None,
        writer: Callable[[Path, bytes], None] | None = None,
    ):
        if not owner:
            raise ValueError("transaction owner must be nonempty")
        self.owner = owner
        self.roots = [Path(root).resolve() for root in roots]
        if not self.roots or len({_root_key(root) for root in self.roots}) != len(self.roots):
            raise ValueError("transaction roots must be unique")
        self.transaction_id = transaction_id or uuid.uuid4().hex
        if not _TRANSACTION_ID.fullmatch(self.transaction_id):
            raise ValueError("invalid transaction identity")
        self.journal_dir = Path(journal_dir).resolve() if journal_dir is not None else None
        self.journal_path = (
            self.journal_dir / f"{self.transaction_id}.json"
            if self.journal_dir is not None else None
        )
        # The standard writer is replaced with an owned-temp implementation so
        # crash recovery can clean only this transaction's interrupted writes.
        self.writer = None if writer in {None, atomic_write} else writer
        self._prepared = False
        self._committed = False
        self._writes: list[tuple[Path, bytes]] = []
        self._entries: list[dict] = []
        self._preconditions: list[tuple[Path, str | None]] = []
        self._precondition_records: list[dict] = []

        seen: set[str] = set()
        for raw_path, raw_data in writes.items():
            path = Path(raw_path).resolve()
            data = bytes(raw_data)
            key = _root_key(path)
            if key in seen:
                raise ValueError(f"duplicate transaction target: {path}")
            seen.add(key)
            location = self._locate(path)
            if location is None:
                raise ValueError(f"transaction target escapes declared roots: {path}")
            if path.exists() and not path.is_file():
                raise ValueError(f"transaction target is not a file: {path}")
            before = path.read_bytes() if path.is_file() else None
            root_index, relative = location
            self._writes.append((path, data))
            self._entries.append({
                "root": root_index,
                "path": relative.as_posix(),
                "existed": before is not None,
                "before_sha256": _sha256(before) if before is not None else None,
                "before_base64": (
                    base64.b64encode(before).decode("ascii")
                    if before is not None else None
                ),
                "after_sha256": _sha256(data),
            })

        write_entries = {
            _root_key(path): entry
            for (path, _data), entry in zip(self._writes, self._entries)
        }
        for raw_path, expected in (preconditions or {}).items():
            path = Path(raw_path).resolve()
            if expected is not None and not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise ValueError(f"invalid precondition digest: {path}")
            location = self._locate(path)
            if location is None:
                raise ValueError(f"precondition escapes declared roots: {path}")
            current = _sha256(path.read_bytes()) if path.is_file() else None
            if current != expected:
                raise FileTransactionError(f"FILE_TRANSACTION_DRIFT: {path}")
            write_entry = write_entries.get(_root_key(path))
            if write_entry is not None:
                if write_entry["before_sha256"] != expected:
                    raise FileTransactionError(f"FILE_TRANSACTION_DRIFT: {path}")
                continue
            root_index, relative = location
            self._preconditions.append((path, expected))
            self._precondition_records.append({
                "root": root_index,
                "path": relative.as_posix(),
                "sha256": expected,
            })

    def _locate(self, path: Path) -> tuple[int, Path] | None:
        matches = []
        for index, root in enumerate(self.roots):
            if path.is_relative_to(root):
                matches.append((len(root.parts), index, path.relative_to(root)))
        if not matches:
            return None
        _depth, index, relative = max(matches)
        return index, relative

    def _record(self, state: str) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "transaction_id": self.transaction_id,
            "owner": self.owner,
            "state": state,
            "roots": [str(root) for root in self.roots],
            "entries": self._entries,
            "preconditions": self._precondition_records,
        }

    def _write_journal(self, state: str) -> None:
        assert self.journal_path is not None
        payload = (
            json.dumps(
                self._record(state), ensure_ascii=False, sort_keys=True,
                indent=2,
            ) + "\n"
        ).encode("utf-8")
        _owned_atomic_write(
            self.journal_path, payload, self.transaction_id + "-journal"
        )

    def _write(self, path: Path, data: bytes) -> None:
        if self.writer is None:
            _owned_atomic_write(path, data, self.transaction_id)
        else:
            self.writer(path, data)

    def _check_preconditions(self) -> None:
        for path, expected in self._preconditions:
            current = _sha256(path.read_bytes()) if path.is_file() else None
            if current != expected:
                raise FileTransactionError(f"FILE_TRANSACTION_DRIFT: {path}")

    def prepare(self) -> None:
        if self._prepared:
            return
        if self.journal_path is not None:
            if self.journal_path.exists():
                raise FileTransactionError(
                    f"FILE_TRANSACTION_EXISTS: {self.journal_path}"
                )
            self._write_journal("PREPARED")
        self._prepared = True

    def commit(self) -> str:
        self.prepare()
        try:
            self._check_preconditions()
            for (path, data), entry in zip(self._writes, self._entries):
                current = _sha256(path.read_bytes()) if path.is_file() else None
                if current != entry["before_sha256"]:
                    raise FileTransactionError(
                        f"FILE_TRANSACTION_DRIFT: {path}"
                    )
                self._write(path, data)
                if not path.is_file() or _sha256(path.read_bytes()) != entry["after_sha256"]:
                    raise FileTransactionError(
                        f"FILE_TRANSACTION_WRITE_MISMATCH: {path}"
                    )
            for path, entry in zip(
                (item[0] for item in self._writes), self._entries
            ):
                if not path.is_file() or _sha256(path.read_bytes()) != entry["after_sha256"]:
                    raise FileTransactionError(
                        f"FILE_TRANSACTION_DRIFT: {path}"
                    )
            self._check_preconditions()
            if self.journal_path is not None:
                self._write_journal("COMMITTED")
            self._committed = True
            return self.transaction_id
        except BaseException:
            try:
                self.rollback()
            except BaseException as rollback_error:
                raise FileTransactionError(
                    "FILE_TRANSACTION_ROLLBACK_FAILED"
                ) from rollback_error
            raise

    def rollback(self) -> None:
        errors = []
        for (path, _data), entry in reversed(list(zip(self._writes, self._entries))):
            try:
                current_digest = _sha256(path.read_bytes()) if path.is_file() else None
                if current_digest not in {
                    entry["before_sha256"], entry["after_sha256"]
                }:
                    raise FileTransactionError(
                        f"FILE_TRANSACTION_DRIFT: {path}"
                    )
                if entry["existed"]:
                    before = base64.b64decode(entry["before_base64"], validate=True)
                    current = path.read_bytes() if path.is_file() else None
                    if current != before:
                        self._write(path, before)
                else:
                    path.unlink(missing_ok=True)
            except BaseException as exc:
                errors.append(exc)
        if errors:
            raise FileTransactionError(
                "FILE_TRANSACTION_ROLLBACK_FAILED"
            ) from errors[0]
        self._committed = False
        self.finalize()

    def finalize(self) -> None:
        if self.journal_path is not None:
            _remove_journal(self.journal_path)


def _load_journal(path: Path, owner: str, roots: list[Path]) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FileTransactionError(
            f"FILE_TRANSACTION_INVALID: unreadable journal {path}"
        ) from exc
    if (
        not isinstance(data, dict)
        or data.get("schema_version") != SCHEMA_VERSION
        or data.get("owner") != owner
        or data.get("state") not in {"PREPARED", "COMMITTED"}
        or not isinstance(data.get("transaction_id"), str)
        or not data["transaction_id"]
        or not isinstance(data.get("roots"), list)
        or not isinstance(data.get("entries"), list)
        or not isinstance(data.get("preconditions"), list)
    ):
        raise FileTransactionError(f"FILE_TRANSACTION_INVALID: {path}")
    if (
        not _TRANSACTION_ID.fullmatch(data["transaction_id"])
        or data["transaction_id"] != path.stem
    ):
        raise FileTransactionError(
            f"FILE_TRANSACTION_ID_MISMATCH: {path}"
        )
    expected_roots = [_root_key(root) for root in roots]
    actual_roots = [_root_key(Path(root)) for root in data["roots"]]
    if actual_roots != expected_roots:
        raise FileTransactionError(
            f"FILE_TRANSACTION_ROOT_MISMATCH: {path}"
        )
    return data


def _journal_entries(data: dict, roots: list[Path]) -> list[tuple[Path, dict, bytes | None]]:
    resolved = []
    for entry in data["entries"]:
        if not isinstance(entry, dict) or type(entry.get("root")) is not int:
            raise FileTransactionError("FILE_TRANSACTION_INVALID: malformed entry")
        root_index = entry["root"]
        if root_index < 0 or root_index >= len(roots):
            raise FileTransactionError("FILE_TRANSACTION_INVALID: unknown root")
        relative_text = entry.get("path")
        relative = PurePosixPath(relative_text) if isinstance(relative_text, str) else None
        if (
            relative is None
            or not relative_text
            or relative.is_absolute()
            or ".." in relative.parts
            or ":" in relative_text
            or type(entry.get("existed")) is not bool
            or not isinstance(entry.get("after_sha256"), str)
        ):
            raise FileTransactionError("FILE_TRANSACTION_INVALID: unsafe entry")
        path = (roots[root_index] / Path(*relative.parts)).resolve()
        if not path.is_relative_to(roots[root_index]):
            raise FileTransactionError("FILE_TRANSACTION_INVALID: escaped entry")
        before = None
        if entry["existed"]:
            try:
                before = base64.b64decode(entry.get("before_base64"), validate=True)
            except (TypeError, ValueError) as exc:
                raise FileTransactionError(
                    "FILE_TRANSACTION_INVALID: bad before-image"
                ) from exc
            if entry.get("before_sha256") != _sha256(before):
                raise FileTransactionError(
                    "FILE_TRANSACTION_INVALID: before-image digest mismatch"
                )
        elif entry.get("before_base64") is not None or entry.get("before_sha256") is not None:
            raise FileTransactionError(
                "FILE_TRANSACTION_INVALID: unexpected before-image"
            )
        resolved.append((path, entry, before))
    return resolved


def _journal_preconditions(data: dict, roots: list[Path]) -> list[tuple[Path, str | None]]:
    resolved = []
    for item in data["preconditions"]:
        if not isinstance(item, dict) or type(item.get("root")) is not int:
            raise FileTransactionError(
                "FILE_TRANSACTION_INVALID: malformed precondition"
            )
        root_index = item["root"]
        if root_index < 0 or root_index >= len(roots):
            raise FileTransactionError(
                "FILE_TRANSACTION_INVALID: unknown precondition root"
            )
        relative_text = item.get("path")
        relative = PurePosixPath(relative_text) if isinstance(relative_text, str) else None
        digest = item.get("sha256")
        if (
            relative is None
            or not relative_text
            or relative.is_absolute()
            or ".." in relative.parts
            or ":" in relative_text
            or (digest is not None and not re.fullmatch(r"[0-9a-f]{64}", digest))
        ):
            raise FileTransactionError(
                "FILE_TRANSACTION_INVALID: unsafe precondition"
            )
        path = (roots[root_index] / Path(*relative.parts)).resolve()
        if not path.is_relative_to(roots[root_index]):
            raise FileTransactionError(
                "FILE_TRANSACTION_INVALID: escaped precondition"
            )
        resolved.append((path, digest))
    return resolved


def recover_file_transactions(
    journal_dir: str | Path,
    *,
    owner: str,
    roots: list[str | Path],
    keep_committed: Callable[[str], bool],
    writer: Callable[[Path, bytes], None] | None = None,
) -> list[tuple[str, str]]:
    """Recover all journals, returning ``(transaction_id, action)`` pairs."""
    directory = Path(journal_dir).resolve()
    if not directory.exists():
        return []
    resolved_roots = [Path(root).resolve() for root in roots]
    actions = []
    for journal in sorted(directory.glob("*.json")):
        data = _load_journal(journal, owner, resolved_roots)
        entries = _journal_entries(data, resolved_roots)
        preconditions = _journal_preconditions(data, resolved_roots)
        transaction_id = data["transaction_id"]

        def restore_write(path: Path, payload: bytes) -> None:
            if writer is None or writer is atomic_write:
                _owned_atomic_write(path, payload, transaction_id + "-recovery")
            else:
                writer(path, payload)

        keep = data["state"] == "COMMITTED" and keep_committed(
            transaction_id
        )
        for path, entry, _before in entries:
            current = _sha256(path.read_bytes()) if path.is_file() else None
            allowed = {entry["after_sha256"], entry["before_sha256"]}
            if current not in allowed:
                raise FileTransactionError(
                    f"FILE_TRANSACTION_DRIFT: {path}"
                )
            if keep and current != entry["after_sha256"]:
                raise FileTransactionError(
                    f"FILE_TRANSACTION_INCOMPLETE_COMMIT: {path}"
                )
        if keep:
            for path, expected in preconditions:
                current = _sha256(path.read_bytes()) if path.is_file() else None
                if current != expected:
                    raise FileTransactionError(
                        f"FILE_TRANSACTION_PRECONDITION_DRIFT: {path}"
                    )
        if keep:
            action = "kept"
        else:
            rollback_errors = []
            for path, entry, before in reversed(entries):
                try:
                    if entry["existed"]:
                        assert before is not None
                        current = path.read_bytes() if path.is_file() else None
                        if current != before:
                            restore_write(path, before)
                    else:
                        path.unlink(missing_ok=True)
                except BaseException as exc:
                    rollback_errors.append(exc)
            if rollback_errors:
                raise FileTransactionError(
                    "FILE_TRANSACTION_ROLLBACK_FAILED"
                ) from rollback_errors[0]
            action = "rolled_back"
        for path, _entry, _before in entries:
            for temporary in path.parent.glob(
                f".run-tx-{transaction_id}-*"
            ):
                try:
                    temporary.unlink()
                except OSError as exc:
                    raise FileTransactionError(
                        f"FILE_TRANSACTION_TEMP_CLEANUP_FAILED: {temporary}"
                    ) from exc
        for temporary in directory.glob(
            f".run-tx-{transaction_id}-journal-*"
        ):
            try:
                temporary.unlink()
            except OSError as exc:
                raise FileTransactionError(
                    f"FILE_TRANSACTION_TEMP_CLEANUP_FAILED: {temporary}"
                ) from exc
        _remove_journal(journal)
        actions.append((transaction_id, action))
    try:
        directory.rmdir()
    except OSError:
        pass
    return actions

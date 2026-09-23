"""Shared run limits, durable progress and owned subprocesses.

The journal records stage status without storing prompts or file contents.
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone


class BudgetExceeded(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class ResumeMismatch(RuntimeError):
    pass


class LockUnavailable(RuntimeError):
    pass


class _WindowsProcessJob:
    """A private Job Object owns only our suspended child and its descendants."""
    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class BASIC_LIMIT(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                         "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class EXTENDED_LIMIT(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC_LIMIT), ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        self.ctypes, self.wintypes = ctypes, wintypes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int,
                                                       ctypes.c_void_p, wintypes.DWORD)
        self.kernel.SetInformationJobObject.restype = wintypes.BOOL
        self.kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        self.kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        self.kernel.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        self.kernel.TerminateJobObject.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = EXTENDED_LIMIT()
        limits.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def attach_and_resume(self, process):
        ctypes, wintypes, kernel = self.ctypes, self.wintypes, self.kernel
        if not kernel.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(process._handle))):
            raise ctypes.WinError(ctypes.get_last_error())

        class THREADENTRY32(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                        ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
                        ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG),
                        ("dwFlags", wintypes.DWORD)]

        kernel.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel.Thread32First.argtypes = kernel.Thread32Next.argtypes = (wintypes.HANDLE, ctypes.POINTER(THREADENTRY32))
        kernel.Thread32First.restype = kernel.Thread32Next.restype = wintypes.BOOL
        kernel.OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenThread.restype = wintypes.HANDLE
        kernel.ResumeThread.argtypes = (wintypes.HANDLE,)
        kernel.ResumeThread.restype = wintypes.DWORD
        snapshot = kernel.CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = THREADENTRY32()
            entry.dwSize = ctypes.sizeof(entry)
            found = kernel.Thread32First(snapshot, ctypes.byref(entry))
            while found:
                if entry.th32OwnerProcessID == process.pid:
                    thread = kernel.OpenThread(0x0002, False, entry.th32ThreadID)  # THREAD_SUSPEND_RESUME
                    if not thread:
                        raise ctypes.WinError(ctypes.get_last_error())
                    try:
                        if kernel.ResumeThread(thread) == 0xFFFFFFFF:
                            raise ctypes.WinError(ctypes.get_last_error())
                        return
                    finally:
                        kernel.CloseHandle(thread)
                found = kernel.Thread32Next(snapshot, ctypes.byref(entry))
            raise RuntimeError("Cannot locate the owned suspended process thread")
        finally:
            kernel.CloseHandle(snapshot)

    def terminate(self):
        if self.handle and not self.kernel.TerminateJobObject(self.handle, 1):
            raise self.ctypes.WinError(self.ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def run_process(args, *, cwd=None, env=None, timeout=600, shell=False,
                capture_output=True, text=True, encoding="utf-8", errors="replace"):
    """Run a private process tree and reap it on timeout, cancellation or exit.

    Windows starts suspended so Job Object assignment precedes any user code.
    POSIX starts a fresh process group. No executable-name or global PID sweep
    is used: cancellation is restricted to this invocation's owned tree.
    """
    check_budget()
    if timeout <= 0:
        raise BudgetExceeded("process_timeout")
    deadline = time.monotonic() + remaining_timeout(timeout)
    job = _WindowsProcessJob() if os.name == "nt" else None
    options = {"cwd": cwd, "env": env, "shell": shell, "text": text}
    if capture_output:
        options.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if text:
        options.update(encoding=encoding, errors=errors)
    if os.name == "nt":
        options["creationflags"] = 0x00000004 | subprocess.CREATE_NO_WINDOW  # CREATE_SUSPENDED
    else:
        options["start_new_session"] = True
    process = None
    def terminate_owned_tree():
        if process is None:
            return
        if job is not None:
            job.terminate()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.kill()  # Own parent only; also handles failed job assignment.
    try:
        process = subprocess.Popen(args, **options)
        if job is not None:
            job.attach_and_resume(process)
        while True:
            check_budget()
            left = deadline - time.monotonic()
            if left <= 0:
                raise BudgetExceeded("process_timeout")
            try:
                stdout, stderr = process.communicate(timeout=min(0.25, remaining_timeout(left)))
                check_budget()
                return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                continue
    except BaseException:
        terminate_owned_tree()
        if process is not None:
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        raise
    finally:
        # Closing the Windows job also removes children left behind after a
        # successful parent exit (Hvigor is run without its shared daemon).
        if job is not None:
            job.close()
        elif process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


class Budget:
    def __init__(self, time_limit_s=None, max_llm_calls=None, max_builds=None):
        self.time_limit_s = time_limit_s
        self.limits = {"llm_calls": max_llm_calls, "builds": max_builds}
        for value in (time_limit_s, *self.limits.values()):
            if value is not None and value < 0:
                raise ValueError("Budget limits must be nonnegative or None")
        self.counts = dict.fromkeys(self.limits, 0)
        self.started = time.monotonic()
        self._previous_elapsed = 0.0
        self._lock = threading.RLock()
        self.snapshot_callback = None

    @property
    def elapsed_s(self):
        return self._previous_elapsed + time.monotonic() - self.started

    def check(self):
        if self.time_limit_s is not None and self.elapsed_s >= self.time_limit_s:
            raise BudgetExceeded("time_limit")

    def consume(self, kind, amount=1):
        aliases = {"llm": "llm_calls", "build": "builds"}
        kind = aliases.get(kind, kind)
        if not isinstance(kind, str) or not kind or amount < 0:
            raise ValueError(f"Invalid budget consumption: {kind}")
        with self._lock:
            self.check()
            self.counts.setdefault(kind, 0)
            limit = self.limits.get(kind)
            if limit is not None and self.counts[kind] + amount > limit:
                raise BudgetExceeded(f"max_{kind}")
            self.counts[kind] += amount
            if self.snapshot_callback is not None:
                self.snapshot_callback(self.snapshot())

    def remaining_timeout(self, default):
        self.check()
        if default <= 0:
            raise ValueError("timeout must be positive")
        if self.time_limit_s is None:
            return float(default)
        return min(float(default), self.time_limit_s - self.elapsed_s)

    def snapshot(self):
        with self._lock:
            return {"time_limit_s": self.time_limit_s, "limits": dict(self.limits),
                    "counts": dict(self.counts), "elapsed_s": self.elapsed_s}

    def restore(self, snapshot):
        """Charge previous active time/counts when continuing a saved run."""
        with self._lock:
            self.counts = {key: int(value) for key, value in snapshot.get("counts", {}).items()}
            for key in self.limits:
                self.counts.setdefault(key, 0)
            if any(value < 0 for value in self.counts.values()):
                raise ValueError("Saved budget counts must be nonnegative")
            self._previous_elapsed = float(snapshot.get("elapsed_s", 0))
            self.started = time.monotonic()
            for kind, limit in self.limits.items():
                if limit is not None and self.counts[kind] > limit:
                    raise BudgetExceeded(f"max_{kind}")
            self.check()
            if self.snapshot_callback is not None:
                self.snapshot_callback(self.snapshot())


_BUDGET = contextvars.ContextVar("run_budget", default=None)
_DEADLINE = contextvars.ContextVar("run_local_deadline", default=None)


@contextlib.contextmanager
def budget_scope(budget):
    token = _BUDGET.set(budget)
    try:
        check_budget()
        yield budget
    finally:
        _BUDGET.reset(token)


def current_budget():
    return _BUDGET.get()


@contextlib.contextmanager
def deadline_scope(seconds):
    """A nested wall-clock limit without replacing the shared counters."""
    if seconds is None:
        yield
        return
    deadline = time.monotonic() + max(0, float(seconds))
    parent = _DEADLINE.get()
    token = _DEADLINE.set(min(parent, deadline) if parent is not None else deadline)
    try:
        check_budget()
        yield
    finally:
        _DEADLINE.reset(token)


def check_budget():
    deadline = _DEADLINE.get()
    if deadline is not None and time.monotonic() >= deadline:
        raise BudgetExceeded("stage_time_limit")
    budget = current_budget()
    if budget is not None:
        budget.check()


def consume_budget(kind, amount=1):
    check_budget()
    budget = current_budget()
    if budget is not None:
        budget.consume(kind, amount)


def remaining_timeout(default):
    check_budget()
    budget = current_budget()
    timeout = budget.remaining_timeout(default) if budget is not None else float(default)
    deadline = _DEADLINE.get()
    if deadline is not None:
        timeout = min(timeout, deadline - time.monotonic())
    if timeout <= 0:
        raise BudgetExceeded("time_limit")
    return timeout


def _now():
    return datetime.now(timezone.utc).isoformat()


_SECRET_KEY = re.compile(r"secret|password|passwd|authorization|api[_-]?key|access[_-]?token|credential", re.I)
_SECRET_TEXT = re.compile(r"(?i)(bearer\s+)[^\s,;]+|\bsk-[A-Za-z0-9_-]{8,}")


def redact(value):
    if isinstance(value, dict):
        return {str(k): "[REDACTED]" if _SECRET_KEY.search(str(k)) else redact(v)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        return _SECRET_TEXT.sub("[REDACTED]", value)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return str(value)


def fingerprint(value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str,
                      separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def file_sha256(path):
    path = Path(path)
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write(path, data):
    """Replace one file, with the temporary file on the destination volume."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".run-tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_json(path, data):
    atomic_write(path, (json.dumps(data, ensure_ascii=False, sort_keys=True,
                                  indent=2) + "\n").encode("utf-8"))


atomic_write_json = atomic_json


class FileLock:
    """OS-owned advisory lock. A killed process releases it automatically.

    Lock files are intentionally retained: deleting one while another process
    holds its inode can create two independent locks on POSIX.
    """
    def __init__(self, path, timeout=0):
        self.path = Path(path)
        self.timeout = timeout
        self._stream = None

    def acquire(self):
        if self._stream is not None:
            raise LockUnavailable(f"Lock already held: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"\0")
            stream.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._stream = stream
                return self
            except OSError as exc:
                if time.monotonic() >= deadline:
                    stream.close()
                    raise LockUnavailable(f"Another run holds {self.path}") from exc
                try:
                    check_budget()
                except BaseException:
                    stream.close()
                    raise
                time.sleep(min(.05, max(0, deadline - time.monotonic())))

    def release(self):
        if self._stream is None:
            return
        stream, self._stream = self._stream, None
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *args):
        self.release()


class RunJournal:
    """Atomic state snapshot plus one atomic JSON file per append-only event."""
    def __init__(self, directory, run_id=None):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.state_path = self.directory / "state.json"
        self._lock_path = self.directory / ".journal.lock"
        with FileLock(self._lock_path, timeout=5):
            if not self.state_path.exists():
                atomic_json(self.state_path, {"run_id": run_id or uuid.uuid4().hex,
                            "status": "created", "created_at": _now(), "stages": {}})
            elif run_id is not None and self.state["run_id"] != run_id:
                raise ResumeMismatch("run_id does not match saved run")
        self.run_id = self.state["run_id"]

    @property
    def state(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def _event_locked(self, name, metadata):
        events = self.directory / "events"
        events.mkdir(exist_ok=True)
        # Recoverable even when a process died after event write, before state.
        number = max((int(p.stem) for p in events.glob("*.json")), default=0) + 1
        event = {"sequence": number, "event": name, "at": _now(),
                 "run_id": self.run_id, "metadata": redact(metadata)}
        atomic_json(events / f"{number:06d}.json", event)
        return event

    def event(self, name, **metadata):
        with FileLock(self._lock_path, timeout=5):
            return self._event_locked(name, metadata)

    def update(self, **changes):
        with FileLock(self._lock_path, timeout=5):
            state = self.state
            state.update(redact(changes))
            state["updated_at"] = _now()
            atomic_json(self.state_path, state)
            return state

    def bind_inputs(self, inputs, resume=False):
        digest = fingerprint(inputs)
        with FileLock(self._lock_path, timeout=5):
            state = self.state
            previous = state.get("input_fingerprint")
            if previous is not None and previous != digest:
                raise ResumeMismatch("Run inputs changed; start a new run")
            if resume and previous is None:
                raise ResumeMismatch("No saved input fingerprint to resume")
            interrupted = [name for name, stage in state.get("stages", {}).items()
                           if stage.get("status") == "running"]
            for name in interrupted:
                state["stages"][name].update(status="interrupted", ended_at=_now())
            state.update(input_fingerprint=digest, inputs=redact(inputs), updated_at=_now())
            self._event_locked("resume" if resume else "inputs_bound",
                               {"fingerprint": digest, "interrupted_stages": interrupted})
            atomic_json(self.state_path, state)
        return digest

    def begin_stage(self, name, **metadata):
        with FileLock(self._lock_path, timeout=5):
            state = self.state
            state.setdefault("stages", {})[name] = {
                "status": "running", "started_at": _now(), "metadata": redact(metadata)}
            state.update(status="running", current_stage=name, updated_at=_now())
            self._event_locked("stage_started", {"stage": name, **metadata})
            atomic_json(self.state_path, state)

    def end_stage(self, name, status="completed", **metadata):
        with FileLock(self._lock_path, timeout=5):
            state = self.state
            stage = state.setdefault("stages", {}).setdefault(name, {})
            stage.update(status=status, ended_at=_now(), result=redact(metadata))
            state.update(updated_at=_now())
            if status in ("failed", "interrupted"):
                state["status"] = status
            self._event_locked("stage_finished", {"stage": name, "status": status, **metadata})
            atomic_json(self.state_path, state)

    @contextlib.contextmanager
    def stage(self, name, **metadata):
        self.begin_stage(name, **metadata)
        try:
            check_budget()
            yield self
            check_budget()
        except BaseException as exc:
            status = "interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "failed"
            self.end_stage(name, status, error_type=type(exc).__name__,
                           reason=getattr(exc, "reason", type(exc).__name__))
            raise
        else:
            self.end_stage(name)


_EXCLUDED_DIRS = {".git", ".hvigor", "build", "oh_modules", "node_modules", ".idea",
                  ".diff_gate", "record_out", "seeds", "tests", "logs", "__pycache__",
                  ".pytest_cache", "transactions", "candidates"}
_EXCLUDED_FILES = {"oracle.py", "oracle.json", "page_pairs.json", "unit_page_map.json",
                   ".pipeline_build.json"}


def _managed(relative):
    parts = Path(relative).parts
    lower = [part.lower() for part in parts]
    return (not any(part in _EXCLUDED_DIRS for part in lower)
            and not lower[-1].startswith((".env", ".run-tmp-"))
            and lower[-1] not in _EXCLUDED_FILES
            and not lower[-1].endswith((".log", ".lock", ".hap", ".hsp", ".pyc")))


def project_manifest(directory):
    directory = Path(directory).resolve()
    result = {}
    for root, dirs, files in os.walk(directory, followlinks=False):
        base = Path(root)
        kept = []
        for name in dirs:
            path = base / name
            if not _managed(path.relative_to(directory)):
                continue
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                raise ValueError(f"Source directory link is not permitted: {path}")
            kept.append(name)
        dirs[:] = kept
        for name in sorted(files):
            path = base / name
            relative = path.relative_to(directory)
            if not _managed(relative):
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(directory):
                raise ValueError(f"Source file escapes project: {path}")
            result[relative.as_posix()] = file_sha256(path)
    return dict(sorted(result.items()))

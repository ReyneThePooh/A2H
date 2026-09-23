"""Real tiny local process trees validate cancellation ownership; no SDK/network."""
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from run_control import Budget, BudgetExceeded, budget_scope, run_process


def alive(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 258  # WAIT_TIMEOUT
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    status = Path(f"/proc/{pid}/stat")
    return not (status.exists() and status.read_text().split()[2] == "Z")


def test_owned_process_returns_output_and_exit_status():
    result = run_process([sys.executable, "-c", "import sys; print('hello'); print('error', file=sys.stderr); sys.exit(4)"],
                         timeout=5)
    assert result.returncode == 4
    assert result.stdout.strip() == "hello" and result.stderr.strip() == "error"


def test_timeout_reaps_only_owned_shell_process_tree(tmp_path):
    script = tmp_path / "tree fixture.py"
    script.write_text("""import os, pathlib, subprocess, sys, time
root = pathlib.Path(sys.argv[1])
depth = int(sys.argv[2])
(root / ('pid-' + str(depth))).write_text(str(os.getpid()))
if depth < 2:
    subprocess.Popen([sys.executable, __file__, str(root), str(depth + 1)])
time.sleep(90)
""", encoding="utf-8")
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(90)"],
                                  creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    own_pids = []
    try:
        argv = [sys.executable, str(script), str(tmp_path), "0"]
        command = subprocess.list2cmdline(argv) if os.name == "nt" else argv
        started = time.monotonic()
        with pytest.raises(BudgetExceeded, match="process_timeout"):
            run_process(command, shell=os.name == "nt", timeout=3)
        assert time.monotonic() - started < 8
        assert all((tmp_path / f"pid-{depth}").exists() for depth in range(3))
        own_pids = [int((tmp_path / f"pid-{depth}").read_text()) for depth in range(3)]
        deadline = time.monotonic() + 3
        while any(alive(pid) for pid in own_pids) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not any(alive(pid) for pid in own_pids)
        assert unrelated.poll() is None
    finally:
        unrelated.kill()
        unrelated.wait(timeout=5)


def test_parent_budget_caps_process_timeout_and_preserves_reason():
    started = time.monotonic()
    with budget_scope(Budget(time_limit_s=0.5)):
        with pytest.raises(BudgetExceeded) as error:
            run_process([sys.executable, "-c", "import time; time.sleep(90)"], timeout=30)
    assert error.value.reason == "time_limit"
    assert time.monotonic() - started < 5


@pytest.mark.skipif(os.name != "nt", reason="Windows suspended job assignment contract")
def test_assignment_failure_never_executes_unowned_child(tmp_path, monkeypatch):
    import run_control
    marker = tmp_path / "must-not-exist"
    def reject(*args):
        raise OSError("fake assignment failure")
    monkeypatch.setattr(run_control._WindowsProcessJob, "attach_and_resume", reject)
    code = "from pathlib import Path; Path(" + repr(str(marker)) + ").write_text('executed')"
    with pytest.raises(OSError, match="assignment failure"):
        run_process([sys.executable, "-c", code], timeout=5)
    assert not marker.exists()

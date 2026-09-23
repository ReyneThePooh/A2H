"""DeviceAdapter 抽象基类 + 子进程调用封装（§5 适配器契约）。

验收要求：任何 hdc/adb 子进程调用必须设超时与重试（1 次），
失败信息包含原始命令与 stderr。
"""
from __future__ import annotations

import logging
import subprocess
import time
from run_control import BudgetExceeded, check_budget, remaining_timeout
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Config
    from ..schemas import AbstractEvent, UNode

logger = logging.getLogger("diff_tester")


class CommandError(RuntimeError):
    """外部命令失败（含原始命令与 stderr）。"""


class LaunchCrashError(RuntimeError):
    """reset_app 冷启动后应用即崩溃/退出（回放器据此直接判 L0_CRASH）。"""

    def __init__(self, message: str, crash_sig: Optional[str] = None,
                 alive: bool = False):
        super().__init__(message)
        self.crash_sig = crash_sig
        self.alive = alive


def run_command(
    args: list[str],
    timeout_s: float = 30.0,
    retries: int = 1,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """执行外部命令（adb/hdc），带超时与重试。"""
    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        check_budget()
        try:
            effective_timeout = remaining_timeout(timeout_s)
            cp = subprocess.run(
                args, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=effective_timeout,
            )
            check_budget()
            if check and cp.returncode != 0:
                raise CommandError(
                    f"命令失败(rc={cp.returncode}): {' '.join(args)}\n"
                    f"stderr: {(cp.stderr or '').strip()[:2000]}"
                )
            return cp
        except subprocess.TimeoutExpired as e:
            check_budget()
            last_err = CommandError(f"命令超时({timeout_s}s): {' '.join(args)}")
            logger.warning("%s (第 %d 次)", last_err, attempt + 1)
        except CommandError as e:
            last_err = e
            logger.warning("%s (第 %d 次)", e, attempt + 1)
        except FileNotFoundError as e:
            raise CommandError(f"找不到可执行文件: {args[0]}（请检查 config 中的路径）") from e
        if attempt < retries:
            check_budget()
            time.sleep(min(1.0, remaining_timeout(1.0)))
    assert last_err is not None
    raise last_err


def run_command_binary(args: list[str], timeout_s: float = 30.0) -> bytes:
    """二进制输出版本（截图等）。"""
    check_budget()
    cp = subprocess.run(args, capture_output=True, timeout=remaining_timeout(timeout_s))
    check_budget()
    if cp.returncode != 0:
        raise CommandError(
            f"命令失败(rc={cp.returncode}): {' '.join(args)}\n"
            f"stderr: {cp.stderr.decode('utf-8', 'replace')[:2000]}"
        )
    return cp.stdout


class DeviceAdapter(ABC):
    """双端统一接口（《AI实现参考1》§5）。"""

    platform: str = "abstract"

    def __init__(self, cfg: "Config"):
        self.cfg = cfg

    # -- 抽象接口 -------------------------------------------------------------

    @abstractmethod
    def ensure_ready(self) -> None:
        """设备连通、被测应用已安装，否则抛异常。"""

    @abstractmethod
    def reset_app(self) -> None:
        """清应用数据 + 冷启动 + 等待首屏稳定。"""

    @abstractmethod
    def dump_tree(self) -> "UNode":
        """dump 并归一化。"""

    @abstractmethod
    def screenshot(self, path: str) -> None: ...

    @abstractmethod
    def current_page(self) -> str:
        """Activity / Ability 名。"""

    @abstractmethod
    def execute(self, ev: "AbstractEvent", node: Optional["UNode"]) -> None:
        """原生执行抽象事件（node 为对齐/录制时定位到的目标节点）。"""

    @abstractmethod
    def poll_crash(self) -> Optional[str]:
        """增量读日志，返回命中的崩溃签名行；无 → None。"""

    @abstractmethod
    def app_alive(self) -> bool:
        """被测应用进程是否存活。"""

    @abstractmethod
    def log_tail(self, n: int = 200) -> str:
        """日志尾 n 行（分叉时收集 artifacts 用）。"""

    # -- 公共实现 -------------------------------------------------------------

    def wait_stable(self, timeout_s: Optional[float] = None) -> bool:
        """界面稳定判据（§4.6）：连续多次 dump 树哈希相同。

        超时仍在变化 → 返回 False（UNSTABLE，用最后一帧参与比较）。
        """
        timeout = remaining_timeout(timeout_s if timeout_s is not None else self.cfg.device.stable_timeout_s)
        interval = self.cfg.device.stable_interval_s
        required_samples = max(2, int(self.cfg.device.stable_samples))
        deadline = time.monotonic() + timeout
        prev: Optional[str] = None
        consecutive = 0
        while time.monotonic() < deadline:
            check_budget()
            try:
                h = self.dump_tree().tree_hash()
            except BudgetExceeded:
                raise
            except Exception as e:
                logger.debug("wait_stable dump 失败: %s", e)
                time.sleep(remaining_timeout(interval))
                continue
            if h == prev:
                consecutive += 1
            else:
                prev = h
                consecutive = 1
            if consecutive >= required_samples:
                return True
            time.sleep(remaining_timeout(interval))
        check_budget()
        return False

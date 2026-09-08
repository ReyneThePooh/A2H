"""AndroidAdapter：uiautomator2 主路径 + adb 子进程兜底。

- dump：`d.dump_hierarchy()`（XML）
- 页面：`d.app_current()["activity"]`
- 崩溃签名：`adb logcat -d -s AndroidRuntime:E ActivityManager:E`，
  维护读取偏移做增量，正则 `FATAL EXCEPTION|ANR in`
- reset_app：`pm clear` + monkey 启动
"""
from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING, Optional

from ..normalize import parse_android_dump
from ..schemas import AbstractEvent, UNode
from .base import CommandError, DeviceAdapter, run_command, run_command_binary

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Config

logger = logging.getLogger("diff_tester")

_CRASH_RE = re.compile(r"FATAL EXCEPTION|ANR in")


class AndroidAdapter(DeviceAdapter):
    platform = "android"

    def __init__(self, cfg: "Config", pkg: str, serial: Optional[str] = None):
        super().__init__(cfg)
        self.pkg = pkg
        self.serial = serial or cfg.device.android_serial
        self._d = None                 # uiautomator2 Device，延迟连接
        self._log_offset = 0
        self._screen: Optional[tuple[int, int]] = None

    # -- 内部工具 ---------------------------------------------------------------

    def _adb_args(self, *args: str) -> list[str]:
        base = [self.cfg.device.adb_path]
        if self.serial:
            base += ["-s", self.serial]
        return base + list(args)

    def _adb(self, *args: str, check: bool = True, timeout: Optional[float] = None):
        return run_command(
            self._adb_args(*args),
            timeout_s=timeout or self.cfg.device.cmd_timeout_s,
            retries=self.cfg.device.cmd_retries,
            check=check,
        )

    @property
    def d(self):
        """uiautomator2 Device（延迟导入 + 连接）。"""
        if self._d is None:
            import uiautomator2 as u2
            self._d = u2.connect(self.serial) if self.serial else u2.connect()
        return self._d

    def _window_size(self) -> tuple[int, int]:
        if self._screen is None:
            try:
                w, h = self.d.window_size()
                self._screen = (int(w), int(h))
            except Exception:
                cp = self._adb("shell", "wm", "size", check=False)
                m = re.search(r"(\d+)x(\d+)", cp.stdout or "")
                self._screen = (int(m.group(1)), int(m.group(2))) if m else (1080, 1920)
        return self._screen

    # -- 契约实现 ---------------------------------------------------------------

    def ensure_ready(self) -> None:
        cp = self._adb("devices")
        lines = [ln for ln in cp.stdout.splitlines()[1:] if ln.strip().endswith("device")]
        if not lines:
            raise CommandError("adb 未检测到任何在线设备（adb devices 为空）")
        if self.serial and not any(self.serial in ln for ln in lines):
            raise CommandError(f"adb 设备 {self.serial} 不在线。当前在线: {lines}")
        cp = self._adb("shell", "pm", "path", self.pkg, check=False)
        if "package:" not in (cp.stdout or ""):
            raise CommandError(f"应用 {self.pkg} 未安装（pm path 无结果）")
        _ = self.d  # 触发 uiautomator2 连接与初始化

    def reset_app(self) -> None:
        self._adb("shell", "pm", "clear", self.pkg, check=False)
        self._adb("shell", "monkey", "-p", self.pkg,
                  "-c", "android.intent.category.LAUNCHER", "1", check=False)
        time.sleep(self.cfg.device.launch_wait_s)
        self.wait_stable()
        self.poll_crash()   # 消费启动前的历史日志，避免误报

    def dump_tree(self) -> UNode:
        try:
            xml = self.d.dump_hierarchy()
        except Exception as e:
            logger.debug("uiautomator2 dump 失败(%s)，走 adb 兜底", e)
            self._adb("shell", "uiautomator", "dump", "/sdcard/_dt_ui.xml", check=False)
            cp = self._adb("shell", "cat", "/sdcard/_dt_ui.xml")
            xml = cp.stdout
        return parse_android_dump(xml, self._window_size(), app_pkg=self.pkg)

    def screenshot(self, path: str) -> None:
        try:
            self.d.screenshot(path)
        except Exception:
            data = run_command_binary(
                self._adb_args("exec-out", "screencap", "-p"),
                timeout_s=self.cfg.device.cmd_timeout_s,
            )
            with open(path, "wb") as f:
                f.write(data)

    def current_page(self) -> str:
        try:
            info = self.d.app_current()
            return info.get("activity") or ""
        except Exception:
            cp = self._adb("shell", "dumpsys", "activity", "activities", check=False)
            m = re.search(r"mResumedActivity.*?([\w.]+)/([\w.$]+)", cp.stdout or "")
            return m.group(2) if m else ""

    def current_pkg(self) -> str:
        try:
            return self.d.app_current().get("package", "")
        except Exception:
            return ""

    def execute(self, ev: AbstractEvent, node: Optional[UNode]) -> None:
        action = ev.action
        sw, sh = self._window_size()
        if action == "CLICK":
            assert node is not None
            x, y = node.center_abs()
            self.d.click(x, y)
        elif action == "LONG_CLICK":
            assert node is not None
            x, y = node.center_abs()
            self.d.long_click(x, y, duration=1.0)
        elif action == "TYPE":
            assert node is not None
            x, y = node.center_abs()
            text = ev.params.get("text", "")
            self.d.click(x, y)
            time.sleep(0.3)
            try:
                self.d.clear_text()
            except Exception:
                pass
            try:
                self.d.send_keys(text)
            except Exception:
                # adb 兜底（不支持非 ASCII）
                self._adb("shell", "input", "text",
                          text.replace(" ", "%s") or '""', check=False)
        elif action == "SWIPE":
            direction = ev.params.get("direction", "up")
            dist = float(ev.params.get("dist", 0.5))
            x1, y1, x2, y2 = _swipe_coords(direction, dist, sw, sh, node)
            self.d.swipe(x1, y1, x2, y2, 0.2)
        elif action == "BACK":
            self.d.press("back")
        elif action == "HOME":
            self.d.press("home")
        elif action == "ROTATE":
            orient = ev.params.get("orientation", "natural")
            try:
                self.d.set_orientation(orient)
            except Exception as e:
                logger.warning("ROTATE 失败: %s", e)
        elif action == "WAIT_IDLE":
            time.sleep(float(ev.params.get("timeout", 2.0)))
        else:
            raise ValueError(f"未知动作: {action}")

    def poll_crash(self) -> Optional[str]:
        cp = self._adb("logcat", "-d", "-s", "AndroidRuntime:E", "ActivityManager:E",
                       check=False)
        lines = (cp.stdout or "").splitlines()
        # 日志缓冲可能被清空/滚动，偏移超界则重置
        start = self._log_offset if self._log_offset <= len(lines) else 0
        new = lines[start:]
        self._log_offset = len(lines)
        for ln in new:
            if _CRASH_RE.search(ln):
                return ln.strip()
        return None

    def app_alive(self) -> bool:
        cp = self._adb("shell", "pidof", self.pkg, check=False)
        return bool((cp.stdout or "").strip())

    def log_tail(self, n: int = 200) -> str:
        cp = self._adb("logcat", "-d", "-t", str(n), check=False)
        return cp.stdout or ""

    def install(self, apk_path: str) -> None:
        logger.info("安装 APK: %s", apk_path)
        self._adb("install", "-r", "-g", apk_path, timeout=180.0)


def _swipe_coords(direction: str, dist: float, sw: int, sh: int,
                  node: Optional[UNode]) -> tuple[int, int, int, int]:
    """SWIPE 起止坐标：有目标节点在其内部滑，否则全屏滑。"""
    if node is not None:
        x1b, y1b, x2b, y2b = node.abs_bounds
        cx, cy = (x1b + x2b) // 2, (y1b + y2b) // 2
        w, h = (x2b - x1b), (y2b - y1b)
    else:
        cx, cy = sw // 2, sh // 2
        w, h = sw, sh
    dx = int(w * dist / 2)
    dy = int(h * dist / 2)
    if direction == "up":
        return cx, cy + dy, cx, cy - dy
    if direction == "down":
        return cx, cy - dy, cx, cy + dy
    if direction == "left":
        return cx + dx, cy, cx - dx, cy
    if direction == "right":
        return cx - dx, cy, cx + dx, cy
    raise ValueError(f"未知滑动方向: {direction}")

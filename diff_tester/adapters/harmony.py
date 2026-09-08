"""HarmonyAdapter：优先 hmdriver2，hdc shell uitest 兜底。

注意：hdc/uitest 在不同鸿蒙版本命令有差异，所有调用点集中封装在本文件，
失败时打印原始命令与 stderr（由 base.run_command 保证）。

- dump：hmdriver2 `dump_hierarchy()` / `uitest dumpLayout`
- 页面：`aa dump -a` 解析当前 Ability；dump 中的 pagePath 作为兜底提示
- 崩溃签名：faultlogger 目录新增文件（cppcrash|jscrash|appfreeze）
- reset_app：`bm clean -n <bundle> -d` + `aa start`
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
import uuid
from typing import TYPE_CHECKING, Any, Optional

from ..normalize import find_harmony_page_hint, parse_harmony_dump
from ..schemas import AbstractEvent, UNode
from .base import CommandError, DeviceAdapter, run_command

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Config

logger = logging.getLogger("diff_tester")

_FAULT_RE = re.compile(r"cppcrash|jscrash|appfreeze", re.IGNORECASE)
_FAULT_DIRS = ("/data/log/faultlog/faultlogger", "/data/log/faultlog/temp")


class HarmonyAdapter(DeviceAdapter):
    platform = "harmony"

    def __init__(self, cfg: "Config", bundle: str, serial: Optional[str] = None):
        super().__init__(cfg)
        self.bundle = bundle
        self.serial = serial or cfg.device.harmony_serial
        self.ability = cfg.device.harmony_ability
        self._hm = None                 # hmdriver2 Driver
        self._hm_broken = False         # hmdriver2 初始化/调用失败后永久降级到 hdc
        self._fault_baseline: Optional[set[str]] = None
        self._last_page_hint: Optional[str] = None

    # -- 内部工具 ---------------------------------------------------------------

    def _hdc_args(self, *args: str) -> list[str]:
        base = [self.cfg.device.hdc_path]
        if self.serial:
            base += ["-t", self.serial]
        return base + list(args)

    def _hdc(self, *args: str, check: bool = True, timeout: Optional[float] = None):
        return run_command(
            self._hdc_args(*args),
            timeout_s=timeout or self.cfg.device.cmd_timeout_s,
            retries=self.cfg.device.cmd_retries,
            check=check,
        )

    def _driver(self):
        """hmdriver2 Driver；不可用返回 None（此后走 hdc 兜底）。"""
        if self._hm_broken:
            return None
        if self._hm is None:
            try:
                from hmdriver2.driver import Driver
                self._hm = Driver(self.serial) if self.serial else Driver()
            except Exception as e:
                logger.warning("hmdriver2 不可用(%s)，全部走 hdc uitest 兜底", e)
                self._hm_broken = True
                return None
        return self._hm

    def _hm_call(self, fn_name: str, *args, **kw):
        """带降级保护的 hmdriver2 调用；失败抛异常由调用方兜底。"""
        drv = self._driver()
        if drv is None:
            raise CommandError("hmdriver2 不可用")
        return getattr(drv, fn_name)(*args, **kw)

    # -- 契约实现 ---------------------------------------------------------------

    def ensure_ready(self) -> None:
        hdc = self.cfg.device.hdc_path
        if not (os.path.isfile(hdc) or _which(hdc)):
            raise CommandError(f"找不到 hdc: {hdc}（请在 config 中指定完整路径）")
        cp = self._hdc("list", "targets")
        targets = [t.strip() for t in (cp.stdout or "").splitlines()
                   if t.strip() and "Empty" not in t]
        if not targets:
            raise CommandError("hdc 未检测到任何设备（hdc list targets 为空）")
        if self.serial and not any(self.serial in t for t in targets):
            raise CommandError(f"hdc 设备 {self.serial} 不在线。当前在线: {targets}")
        cp = self._hdc("shell", "bm", "dump", "-n", self.bundle, check=False)
        if self.bundle not in (cp.stdout or ""):
            raise CommandError(f"应用 {self.bundle} 未安装（bm dump 无结果）")

    def reset_app(self) -> None:
        self._hdc("shell", "bm", "clean", "-n", self.bundle, "-d", check=False)
        self._hdc("shell", "aa", "force-stop", self.bundle, check=False)
        self._hdc("shell", "aa", "start", "-a", self.ability, "-b", self.bundle,
                  check=False)
        time.sleep(self.cfg.device.launch_wait_s)
        self.wait_stable()
        self._snapshot_faults()   # 重置崩溃基线

    def dump_tree(self) -> UNode:
        data = self._dump_raw()
        self._last_page_hint = find_harmony_page_hint(data)
        return parse_harmony_dump(data)

    def _dump_raw(self) -> dict[str, Any]:
        # 主路径：hmdriver2
        if not self._hm_broken:
            try:
                data = self._hm_call("dump_hierarchy")
                if isinstance(data, dict) and data:
                    return data
            except Exception as e:
                logger.debug("hmdriver2 dump 失败(%s)，走 hdc 兜底", e)
        # 兜底：uitest dumpLayout 到设备文件再取回
        remote = f"/data/local/tmp/_dt_layout_{uuid.uuid4().hex[:8]}.json"
        cp = self._hdc("shell", "uitest", "dumpLayout", "-p", remote)
        # 部分版本把实际输出路径打印在 stdout（DumpLayout saved to:xxx）
        m = re.search(r"saved to\s*:?\s*(\S+\.json)", cp.stdout or "", re.IGNORECASE)
        if m:
            remote = m.group(1)
        local = os.path.join(tempfile.gettempdir(), os.path.basename(remote))
        self._hdc("file", "recv", remote, local)
        self._hdc("shell", "rm", "-f", remote, check=False)
        with open(local, "r", encoding="utf-8", errors="replace") as f:
            return json.load(f)

    def screenshot(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        if not self._hm_broken:
            try:
                self._hm_call("screenshot", path)
                return
            except Exception as e:
                logger.debug("hmdriver2 截图失败(%s)，走 hdc 兜底", e)
        remote = f"/data/local/tmp/_dt_shot_{uuid.uuid4().hex[:8]}.png"
        cp = self._hdc("shell", "uitest", "screenCap", "-p", remote, check=False)
        if cp.returncode != 0:
            # 旧版本命令
            self._hdc("shell", "snapshot_display", "-f", remote)
        self._hdc("file", "recv", remote, path)
        self._hdc("shell", "rm", "-f", remote, check=False)

    def current_page(self) -> str:
        """当前 Ability 名；只取被测 bundle 的前台 PAGE Ability。"""
        cp = self._hdc("shell", "aa", "dump", "-a", check=False, timeout=15)
        out = cp.stdout or ""
        ability = self._parse_foreground_ability(out)
        if ability and self._last_page_hint:
            return f"{ability}:{self._last_page_hint}"
        return ability or (self._last_page_hint or "")

    def _parse_foreground_ability(self, dump: str) -> str:
        """从 aa dump -a 中解析被测应用的前台 PAGE Ability。

        全量 dump 含大量系统 Extension/Service，不能取最后一个 main name。
        """
        # 1) 优先：mission name 里带 bundle 且 AbilityRecord 为 FOREGROUND PAGE
        blocks = re.split(r"\n\s*AbilityRecord ID", dump)
        for block in blocks:
            if self.bundle not in block:
                continue
            if not re.search(r"ability type \[PAGE\]", block):
                continue
            if not re.search(r"state #FOREGROUND", block):
                continue
            m = re.search(r"main name \[([\w.]+)\]", block)
            if m:
                return m.group(1)
        # 2) mission name #[#bundle:module:Ability]
        m = re.search(
            rf"mission name\s*#\[#?{re.escape(self.bundle)}:[^:\]]+:([\w.]+)\]",
            dump,
        )
        if m:
            return m.group(1)
        # 3) bundle 段内任意 PAGE
        m = re.search(
            rf"bundle name \[{re.escape(self.bundle)}\][\s\S]{{0,200}}main name \[([\w.]+)\]",
            dump,
        )
        return m.group(1) if m else ""

    def execute(self, ev: AbstractEvent, node: Optional[UNode]) -> None:
        action = ev.action
        if action == "CLICK":
            assert node is not None
            x, y = node.center_abs()
            self._input("click", str(x), str(y), hm=("click", x, y))
        elif action == "LONG_CLICK":
            assert node is not None
            x, y = node.center_abs()
            self._input("longClick", str(x), str(y), hm=("long_click", x, y))
        elif action == "TYPE":
            assert node is not None
            x, y = node.center_abs()
            text = ev.params.get("text", "")
            self._input("click", str(x), str(y), hm=("click", x, y))
            time.sleep(0.3)
            if text == "":
                return
            self._input("inputText", str(x), str(y), text, hm=("input_text", text))
        elif action == "SWIPE":
            direction = ev.params.get("direction", "up")
            dist = float(ev.params.get("dist", 0.5))
            tree_root = node  # 有目标节点在其内部滑
            sw, sh = self._screen_size()
            from .android import _swipe_coords
            x1, y1, x2, y2 = _swipe_coords(direction, dist, sw, sh, tree_root)
            self._input("swipe", str(x1), str(y1), str(x2), str(y2), "600",
                        hm=("swipe", x1, y1, x2, y2))
        elif action == "BACK":
            self._input("keyEvent", "Back", hm=("go_back",))
        elif action == "HOME":
            self._input("keyEvent", "Home", hm=("go_home",))
        elif action == "ROTATE":
            logger.warning("鸿蒙 ROTATE 暂未实现，跳过")
        elif action == "WAIT_IDLE":
            time.sleep(float(ev.params.get("timeout", 2.0)))
        else:
            raise ValueError(f"未知动作: {action}")

    def _input(self, *uitest_args: str, hm: Optional[tuple] = None) -> None:
        """事件注入：优先 hmdriver2（若声明了 hm 调用），失败走 uitest uiInput。"""
        if hm and not self._hm_broken:
            try:
                self._hm_call(hm[0], *hm[1:])
                return
            except Exception as e:
                logger.debug("hmdriver2 %s 失败(%s)，走 uitest 兜底", hm[0], e)
        self._hdc("shell", "uitest", "uiInput", *uitest_args)

    def _screen_size(self) -> tuple[int, int]:
        try:
            tree = self.dump_tree()
            return tree.abs_bounds[2] or 1080, tree.abs_bounds[3] or 2340
        except Exception:
            return 1080, 2340

    def poll_crash(self) -> Optional[str]:
        """faultlogger 目录增量检测新崩溃文件。"""
        files = self._list_faults()
        if self._fault_baseline is None:
            self._fault_baseline = files
            return None
        new = files - self._fault_baseline
        self._fault_baseline = files
        for name in sorted(new):
            if _FAULT_RE.search(name) and self.bundle in name:
                return name
        # 同类型崩溃但文件名不含 bundle 的，谨慎起见也报（带标记）
        for name in sorted(new):
            if _FAULT_RE.search(name):
                return f"{name} (bundle未确认)"
        return None

    def _list_faults(self) -> set[str]:
        out: set[str] = set()
        for d in _FAULT_DIRS:
            cp = self._hdc("shell", "ls", d, check=False)
            if cp.returncode == 0:
                out |= {w for w in (cp.stdout or "").split() if w and "No such" not in w}
        return out

    def _snapshot_faults(self) -> None:
        self._fault_baseline = self._list_faults()

    def app_alive(self) -> bool:
        cp = self._hdc("shell", f"pidof {self.bundle}", check=False)
        if (cp.stdout or "").strip():
            return True
        cp = self._hdc("shell", f"ps -ef | grep {self.bundle} | grep -v grep",
                       check=False)
        return bool((cp.stdout or "").strip())

    def log_tail(self, n: int = 200) -> str:
        cp = self._hdc("shell", "hilog", "-x", check=False, timeout=30)
        lines = (cp.stdout or "").splitlines()
        return "\n".join(lines[-n:])

    def install(self, hap_path: str) -> None:
        logger.info("安装 HAP: %s", hap_path)
        cp = self._hdc("install", hap_path, check=False, timeout=180.0)
        out = (cp.stdout or "") + (cp.stderr or "")
        if cp.returncode != 0 or "fail" in out.lower():
            # 部分版本用 app install
            cp2 = self._hdc("app", "install", hap_path, check=False, timeout=180.0)
            out2 = (cp2.stdout or "") + (cp2.stderr or "")
            if cp2.returncode != 0 or "fail" in out2.lower():
                raise CommandError(f"HAP 安装失败:\n{out}\n{out2}")


def _which(name: str) -> Optional[str]:
    import shutil
    return shutil.which(name)

"""状态抽象函数 alpha、文本掩码、页面指纹（§6.1）。

核心是纯函数 `build_state_vector`（树 → StateVector），便于单测；
`alpha(adapter, ...)` 是面向设备的封装：dump + 截图 + 崩溃轮询 + 落盘附件。
"""
from __future__ import annotations

import math
import os
import re
from collections import Counter
from typing import TYPE_CHECKING, Optional

from .config import DEFAULT_MASK_PATTERNS
from .schemas import StateVector, UNode

if TYPE_CHECKING:  # pragma: no cover
    from .adapters.base import DeviceAdapter

VOLATILE = "<VOLATILE>"

#: 参与 widgets 摘要的角色
_WIDGET_ROLES = ("button", "textfield", "checkbox", "switch", "listitem")

#: 状态栏 / 系统通知（跨端不可比，不进入 texts）
_CHROME_TEXT = re.compile(
    r"(notification:|Wifi signal|Phone signal|Battery \d|Android System|"
    r"Play Protect|Digital Wellbeing|screen lock|Virtual SD card|"
    r"Configure physical keyboard|signal full)",
    re.IGNORECASE,
)
_VOLATILE_ONLY = re.compile(rf"^(?:{re.escape(VOLATILE)}(?:\s*[AP]M)?\s*)+$", re.IGNORECASE)


def compile_masks(patterns: Optional[list[str]] = None) -> list[re.Pattern]:
    return [re.compile(p) for p in (patterns if patterns is not None else DEFAULT_MASK_PATTERNS)]


def mask_text(s: str, compiled: list[re.Pattern]) -> str:
    """易变串（时间/日期/长数字/百分比）→ 占位符。"""
    for p in compiled:
        s = p.sub(VOLATILE, s)
    return s.strip()


def build_state_vector(
    tree: UNode,
    page: str,
    masks: list[re.Pattern],
    *,
    alive: bool = True,
    crash_sig: Optional[str] = None,
    screenshot: str = "",
    dump_path: str = "",
) -> StateVector:
    """UNode 树 + 页面名 → 语义状态向量（纯函数）。"""
    texts: Counter[str] = Counter()
    widgets: dict[str, list[str]] = {}
    values: dict[str, str] = {}
    list_counts: dict[str, int] = {}
    role_seq: Counter[str] = Counter()   # 各 role 的序号计数（无 id 时用作 key）

    for n in tree.iter_all():
        if _is_system_chrome(n):
            continue
        if n.text:
            t = mask_text(n.text, masks)
            if t and not _VOLATILE_ONLY.match(t):
                texts[t] += 1
        if n.desc:
            t = mask_text(n.desc, masks)
            if t and not _VOLATILE_ONLY.match(t):
                texts[t] += 1

        idx = role_seq[n.role]
        role_seq[n.role] += 1
        key = n.id or f"{n.role}#{idx}"

        if n.role == "textfield":
            values[key] = mask_text(n.text, masks)
        elif n.role in ("checkbox", "switch") and n.checked is not None:
            values[key] = str(n.checked)
        elif n.role == "list":
            list_counts[key] = len(n.children)

    for n in tree.iter_interactive():
        if n.role in _WIDGET_ROLES:
            label = mask_text(n.text or n.desc, masks)
            widgets.setdefault(n.role, []).append(label)
    for r in widgets:
        widgets[r].sort()

    texts.pop("", None)

    return StateVector(
        page=page,
        texts=dict(texts),
        widgets=widgets,
        values=values,
        list_counts=list_counts,
        alive=alive,
        crash_sig=crash_sig,
        screenshot=screenshot,
        dump_path=dump_path,
    )


def _is_system_chrome(n: UNode) -> bool:
    """状态栏、系统通知等跨端噪声节点。"""
    # 整个节点都落在屏幕顶部 6% 以内 → 系统栏（应用标题通常更靠下）
    if n.rel_bounds[3] < 0.06:
        return True
    blob = f"{n.text} {n.desc}"
    return bool(_CHROME_TEXT.search(blob))


def alpha(
    adapter: "DeviceAdapter",
    masks: list[re.Pattern],
    artifacts_dir: Optional[str] = None,
    tag: str = "state",
) -> StateVector:
    """面向设备的状态抽象：dump + 页面名 + 崩溃轮询 (+ 截图/dump 附件落盘)。"""
    tree = adapter.dump_tree()
    page = adapter.current_page()
    crash_sig = adapter.poll_crash()
    alive = adapter.app_alive()

    screenshot_path = ""
    dump_path = ""
    if artifacts_dir:
        os.makedirs(artifacts_dir, exist_ok=True)
        screenshot_path = os.path.join(artifacts_dir, f"{tag}.png")
        try:
            adapter.screenshot(screenshot_path)
        except Exception:   # 截图失败不阻断状态抽象（只是附件）
            screenshot_path = ""
        dump_path = os.path.join(artifacts_dir, f"{tag}_dump.json")
        from .schemas import save_json
        save_json(tree.to_dict(), dump_path)

    return build_state_vector(
        tree, page, masks,
        alive=alive, crash_sig=crash_sig,
        screenshot=screenshot_path, dump_path=dump_path,
    )


# ---------------------------------------------------------------------------
# 页面指纹（用于自动建页面对映射表 page_pairs.json）
# ---------------------------------------------------------------------------

def page_fingerprint(tree: UNode, masks: Optional[list[re.Pattern]] = None) -> dict:
    """fingerprint(page) = (标题区文本, 各 role 控件数向量)。"""
    masks = masks or compile_masks()
    titles: list[str] = []
    for n in tree.iter_all():
        _, cy = n.center_rel()
        if n.text and cy < 0.15:
            t = mask_text(n.text, masks)
            if t:
                titles.append(t)
        if len(titles) >= 5:
            break
    role_counts = Counter(n.role for n in tree.iter_all())
    return {"title_texts": titles, "role_counts": dict(role_counts)}


def _cosine(a: dict[str, int], b: dict[str, int]) -> float:
    keys = set(a) | set(b)
    dot = sum(a.get(k, 0) * b.get(k, 0) for k in keys)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def pair_pages(
    fps_android: dict[str, dict],
    fps_harmony: dict[str, dict],
    min_score: float = 0.5,
) -> dict[str, str]:
    """按指纹相似度做贪心二分图匹配，产出 安卓页面 → 鸿蒙页面 映射。

    返回结果应人工确认一遍后落盘 page_pairs.json（§6.1）。
    """
    from rapidfuzz import fuzz

    def title_sim(t1: list[str], t2: list[str]) -> float:
        if not t1 or not t2:
            return 0.0
        return max(fuzz.ratio(x, y) / 100.0 for x in t1 for y in t2)

    scored: list[tuple[float, str, str]] = []
    for pa, fa in fps_android.items():
        for ph, fh in fps_harmony.items():
            s = 0.5 * _cosine(fa.get("role_counts", {}), fh.get("role_counts", {})) \
                + 0.5 * title_sim(fa.get("title_texts", []), fh.get("title_texts", []))
            scored.append((s, pa, ph))
    scored.sort(reverse=True)

    mapping: dict[str, str] = {}
    used_h: set[str] = set()
    for s, pa, ph in scored:
        if s < min_score:
            break
        if pa in mapping or ph in used_h:
            continue
        mapping[pa] = ph
        used_h.add(ph)
    return mapping

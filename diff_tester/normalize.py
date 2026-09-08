"""两端 dump → 统一控件树 UNode（M1 归一化层）。

- 安卓：uiautomator dump 的 XML（uiautomator2 `dump_hierarchy()` 同格式）；
- 鸿蒙：uitest dumpLayout / hmdriver2 `dump_hierarchy()` 的 JSON。

角色映射表内置、可配置扩充（规则为"class/type 子串 → role"，顺序敏感：先具体后一般）。
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, Optional

from .schemas import Bounds, UNode

_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")

# ---------------------------------------------------------------------------
# 角色映射（《AI实现参考1》§4.1 表）
# ---------------------------------------------------------------------------

#: 安卓 class 子串 → role（顺序敏感）
ANDROID_ROLE_RULES: list[tuple[str, str]] = [
    ("EditText", "textfield"),
    ("AutoComplete", "textfield"),
    ("SearchView", "textfield"),
    ("CheckBox", "checkbox"),
    ("CheckedTextView", "checkbox"),
    ("Switch", "switch"),
    ("Toggle", "switch"),          # ToggleButton 含 "Button"，须在 Button 之前
    ("ImageButton", "button"),
    ("Button", "button"),
    ("RecyclerView", "list"),
    ("ListView", "list"),
    ("GridView", "list"),
    ("ScrollView", "list"),
    ("ViewPager", "list"),
    ("ImageView", "image"),
    ("TextView", "text"),          # 不可点击→text；可点击→button（见 _android_role）
]

#: 鸿蒙 type 子串 → role（顺序敏感）
HARMONY_ROLE_RULES: list[tuple[str, str]] = [
    ("TextInput", "textfield"),
    ("TextArea", "textfield"),
    ("SearchField", "textfield"),
    ("Search", "textfield"),
    ("Checkbox", "checkbox"),
    ("Toggle", "switch"),
    ("Switch", "switch"),
    ("Button", "button"),
    ("ListItem", "listitem"),      # 须在 List 之前
    ("GridItem", "listitem"),      # 须在 Grid 之前
    ("List", "list"),
    ("Grid", "list"),
    ("Scroll", "list"),
    ("Swiper", "list"),
    ("WaterFlow", "list"),
    ("Image", "image"),
    ("Text", "text"),
]


def _match_role(name: str, rules: list[tuple[str, str]], clickable: bool) -> Optional[str]:
    for sub, role in rules:
        if sub in name:
            if role == "text" and clickable:
                return "button"    # 可点击的纯文本按语义视为按钮
            return role
    return None


def _rel(bounds: Bounds, sw: int, sh: int) -> tuple[float, float, float, float]:
    sw = max(1, sw)
    sh = max(1, sh)
    x1, y1, x2, y2 = bounds

    def cl(v: float) -> float:
        return max(0.0, min(1.0, v))

    return (cl(x1 / sw), cl(y1 / sh), cl(x2 / sw), cl(y2 / sh))


def _parse_bounds(s: str) -> Bounds:
    m = _BOUNDS_RE.search(s or "")
    if not m:
        return (0, 0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))


def _mark_listitems(root: UNode) -> None:
    """后处理：list 容器的直接子节点若无更具体角色，标记为 listitem。"""
    for n in root.iter_all():
        if n.role == "list":
            for c in n.children:
                if c.role in ("container", "other"):
                    c.role = "listitem"


# ---------------------------------------------------------------------------
# 安卓
# ---------------------------------------------------------------------------

def parse_android_dump(
    xml_str: str,
    screen: Optional[tuple[int, int]] = None,
    app_pkg: Optional[str] = None,
) -> UNode:
    """uiautomator XML → UNode 树。

    screen 为 (宽, 高)；缺省时用根节点 bounds 推断。
    app_pkg 若给定，丢弃其他 package 的子树（状态栏 / 通知 / 导航栏）。
    """
    root_el = ET.fromstring(xml_str)
    if root_el.tag == "node":
        node_els = [root_el]
    else:  # <hierarchy>
        node_els = [c for c in root_el if c.tag == "node"]
    if not node_els:
        raise ValueError("安卓 dump 中没有任何 <node> 节点")

    if screen is None:
        b = _parse_bounds(node_els[0].attrib.get("bounds", ""))
        screen = (b[2] or 1080, b[3] or 1920)
    sw, sh = screen

    children = [n for n in (_build_android(el, sw, sh, app_pkg) for el in node_els) if n]
    if len(children) == 1:
        root = children[0]
    else:
        root = UNode(role="container", id=None, text="", desc="",
                     abs_bounds=(0, 0, sw, sh), rel_bounds=(0.0, 0.0, 1.0, 1.0),
                     clickable=False, editable=False, checked=None, children=children)
    _mark_listitems(root)
    return root


def _build_android(el: ET.Element, sw: int, sh: int, app_pkg: Optional[str] = None) -> Optional[UNode]:
    a = el.attrib
    pkg = a.get("package", "") or ""
    if app_pkg and pkg and pkg != app_pkg:
        return None
    cls = a.get("class", "")
    clickable = a.get("clickable") == "true" or a.get("long-clickable") == "true"
    bounds = _parse_bounds(a.get("bounds", ""))

    rid = a.get("resource-id", "") or ""
    if ":id/" in rid:
        rid = rid.split(":id/", 1)[1]          # 去掉 "pkg:id/" 前缀，取尾段
    node_id = rid or None

    checked: Optional[bool] = None
    if a.get("checkable") == "true":
        checked = a.get("checked") == "true"

    children = [n for n in (_build_android(c, sw, sh, app_pkg) for c in el if c.tag == "node") if n]
    role = _match_role(cls, ANDROID_ROLE_RULES, clickable)
    if role is None:
        role = "container" if children else "other"

    return UNode(
        role=role,
        id=node_id,
        text=a.get("text", "") or "",
        desc=a.get("content-desc", "") or "",
        abs_bounds=bounds,
        rel_bounds=_rel(bounds, sw, sh),
        clickable=clickable,
        editable=(role == "textfield"),
        checked=checked,
        children=children,
    )


# ---------------------------------------------------------------------------
# 鸿蒙
# ---------------------------------------------------------------------------

def parse_harmony_dump(data: dict[str, Any], screen: Optional[tuple[int, int]] = None) -> UNode:
    """uitest dumpLayout / hmdriver2 JSON → UNode 树。"""
    attrs = _hm_attrs(data)
    if screen is None:
        b = _parse_bounds(str(attrs.get("bounds", "")))
        screen = (b[2] or 1080, b[3] or 2340)
    root = _build_harmony(data, screen[0], screen[1])
    _mark_listitems(root)
    return root


def _hm_attrs(node: dict[str, Any]) -> dict[str, Any]:
    return node.get("attributes") or node.get("Attributes") or {}


def _hm_children(node: dict[str, Any]) -> list[dict[str, Any]]:
    return node.get("children") or node.get("Children") or []


def _build_harmony(node: dict[str, Any], sw: int, sh: int) -> UNode:
    a = _hm_attrs(node)
    typ = str(a.get("type", ""))
    clickable = str(a.get("clickable", "")).lower() == "true" \
        or str(a.get("longClickable", "")).lower() == "true"
    bounds = _parse_bounds(str(a.get("bounds", "")))

    node_id = str(a.get("id", "") or a.get("key", "") or "") or None
    text = str(a.get("text", "") or "")
    desc = str(a.get("description", "") or "")

    children = [_build_harmony(c, sw, sh) for c in _hm_children(node)]
    role = _match_role(typ, HARMONY_ROLE_RULES, clickable)
    if role is None:
        role = "container" if children else "other"

    checked: Optional[bool] = None
    if role in ("checkbox", "switch"):
        checked = str(a.get("checked", "")).lower() == "true"

    return UNode(
        role=role,
        id=node_id,
        text=text,
        desc=desc,
        abs_bounds=bounds,
        rel_bounds=_rel(bounds, sw, sh),
        clickable=clickable,
        editable=(role == "textfield"),
        checked=checked,
        children=children,
    )


def find_harmony_page_hint(data: dict[str, Any]) -> Optional[str]:
    """在鸿蒙 dump JSON 中尽力提取页面路由信息（pagePath / navDestination）。"""
    attrs = _hm_attrs(data)
    for key in ("pagePath", "navDestination", "pageName"):
        v = attrs.get(key)
        if v:
            return str(v)
    for c in _hm_children(data):
        hint = find_harmony_page_hint(c)
        if hint:
            return hint
    return None

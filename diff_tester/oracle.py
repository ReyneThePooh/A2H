"""分层预言 L0–L3（§6.3）。

逐层短路：上层失败即报分叉，不再看下层。
  L0 存活性 → L0_CRASH
  L1 页面身份 → L1_PAGE
  L2 语义内容（文本 Jaccard / 控件对齐率 / 值状态） → L2_CONTENT
  L3 视觉参考（感知哈希距离，仅记录，不判失败）
"""
from __future__ import annotations

import json
import os
import re
from typing import Optional

from rapidfuzz import fuzz

from .config import OracleConfig
from .schemas import OracleVerdict, StateVector, load_json

_PAGE_SUFFIXES = ("activity", "ability", "abilitystage", "fragment", "page", "view")


class PagePairs:
    """页面对映射表：安卓页面名 ↔ 鸿蒙页面名。

    - 显式映射来自 page_pairs.json（{"安卓页面": "鸿蒙页面"} 或 {"pairs": [[a,h],...]})；
    - 表中无条目时退化为启发式：去掉 Activity/Ability 等后缀取词干做模糊比较。
    """

    def __init__(self, mapping: Optional[dict[str, str]] = None,
                 heuristic: bool = True, stem_sim_min: float = 0.75):
        self.mapping = dict(mapping or {})
        self.heuristic = heuristic
        self.stem_sim_min = stem_sim_min

    @classmethod
    def load(cls, path: Optional[str], **kw) -> "PagePairs":
        if not path:
            return cls(**kw)
        data = load_json(path)
        if isinstance(data, dict) and "pairs" in data:
            mapping = {a: h for a, h in data["pairs"]}
        elif isinstance(data, dict):
            mapping = data
        else:
            raise ValueError(f"page_pairs 文件格式不支持: {path}")
        return cls(mapping=mapping, **kw)

    @staticmethod
    def _stem(name: str) -> str:
        # 取最后一段（类名/路由名），去掉平台后缀，规整大小写
        seg = re.split(r"[./\\:#]", name.strip())[-1]
        seg = seg.strip().lower().replace("_", "").replace("-", "")
        for suf in _PAGE_SUFFIXES:
            if seg.endswith(suf) and len(seg) > len(suf):
                seg = seg[: -len(suf)]
                break
        return seg

    def corresponds(self, android_page: str, harmony_page: str) -> bool:
        if android_page in self.mapping:
            mapped = self.mapping[android_page]
            if mapped == harmony_page:
                return True
            # 映射写 Ability，实际页可能是 Ability:pages/Xxx
            if harmony_page.startswith(mapped + ":"):
                return True
            if mapped == harmony_page.split(":")[0]:
                return True
            return False
        if android_page == harmony_page:
            return True
        if not self.heuristic:
            return False
        sa, sh = self._stem(android_page), self._stem(harmony_page)
        if not sa or not sh:
            return False
        if sa == sh:
            return True
        return fuzz.ratio(sa, sh) / 100.0 >= self.stem_sim_min


# ---------------------------------------------------------------------------
# L2 子谓词
# ---------------------------------------------------------------------------

def _fold_multiset(d: dict[str, int]) -> dict[str, int]:
    """按 casefold 归并文本多重集：吸收安卓 textAllCaps 等平台渲染差异。"""
    out: dict[str, int] = {}
    for k, n in d.items():
        kk = k.casefold()
        out[kk] = out.get(kk, 0) + n
    return out


def multiset_jaccard(a: dict[str, int], b: dict[str, int]) -> float:
    """multiset Jaccard：J = Σ min(a,b) / Σ max(a,b)；两者皆空 → 1.0。"""
    keys = set(a) | set(b)
    if not keys:
        return 1.0
    inter = sum(min(a.get(k, 0), b.get(k, 0)) for k in keys)
    union = sum(max(a.get(k, 0), b.get(k, 0)) for k in keys)
    return inter / union if union else 1.0


def widget_align_rate(
    expected: dict[str, list[str]],
    actual: dict[str, list[str]],
    text_sim_min: float = 0.8,
    page_texts: Optional[dict[str, int]] = None,
) -> float:
    """expected.widgets 中控件在 actual 找到匹配的比例（text+role 贪心对齐）。

    page_texts（casefold 后的页面文本多重集）作最后兜底：控件标签渲染结构
    跨端漂移（如安卓 RadioButton 自带标签 vs 鸿蒙 Radio+旁侧 Text），
    只要标签文本仍在页面上且存在可交互控件，即视为对齐。
    """
    total = 0
    aligned = 0
    for role, labels in expected.items():
        pool = list(actual.get(role, []))
        # 兼容角色的候选并入池（checkbox↔switch、button↔text 等常见漂移）
        for r2, labs2 in actual.items():
            if r2 != role and frozenset((role, r2)) in _L2_COMPAT:
                pool.extend(labs2)
        for lab in labels:
            total += 1
            best_i, best_s = -1, -1.0
            for i, cand in enumerate(pool):
                s = fuzz.ratio(lab.casefold(), cand.casefold()) / 100.0
                if s > best_s:
                    best_i, best_s = i, s
            if best_i >= 0 and best_s >= text_sim_min:
                aligned += 1
                pool.pop(best_i)
            elif page_texts:
                lf = lab.casefold()
                if any(fuzz.ratio(lf, k) / 100.0 >= text_sim_min
                       for k in page_texts):
                    aligned += 1
    return aligned / total if total else 1.0


_L2_COMPAT: set[frozenset[str]] = {
    frozenset(("button", "text")),
    frozenset(("button", "listitem")),
    frozenset(("checkbox", "switch")),
}


def compare_keyed(
    expected: dict, actual: dict, fuzzy_key_min: float = 0.6
) -> list[dict]:
    """values / list_counts 的逐 key 比较（key 经对齐后比较）。

    先精确 key 匹配，再模糊 key 匹配（两端 id/序号命名可能漂移），
    返回不一致项列表；仅 actual 多出的 key 不计（由文本/控件层捕捉）。
    """
    mismatches: list[dict] = []
    unused = dict(actual)
    for k, v in expected.items():
        if k in unused:
            av = unused.pop(k)
        else:
            best_k, best_s = None, 0.0
            for k2 in unused:
                s = fuzz.ratio(k.casefold(), k2.casefold()) / 100.0
                if s > best_s:
                    best_k, best_s = k2, s
            if best_k is not None and best_s >= fuzzy_key_min:
                av = unused.pop(best_k)
            else:
                mismatches.append({"key": k, "expected": v, "actual": None})
                continue
        if str(av) != str(v):
            mismatches.append({"key": k, "expected": v, "actual": av})
    return mismatches


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def compare(
    expected: StateVector,
    actual: StateVector,
    page_pairs: PagePairs,
    cfg: Optional[OracleConfig] = None,
) -> OracleVerdict:
    """分层比较：期望值（安卓基线）vs 实际值（鸿蒙回放）。"""
    cfg = cfg or OracleConfig()

    # ---- L0 存活性 ----------------------------------------------------------
    if not actual.alive or actual.crash_sig is not None:
        return OracleVerdict(passed=False, kind="L0_CRASH", detail={
            "alive": actual.alive,
            "crash_sig": actual.crash_sig,
        })

    # ---- L1 页面身份 --------------------------------------------------------
    if not page_pairs.corresponds(expected.page, actual.page):
        return OracleVerdict(passed=False, kind="L1_PAGE", detail={
            "expected_page": expected.page,
            "actual_page": actual.page,
        })

    # ---- L2 语义内容 --------------------------------------------------------
    exp_texts = _fold_multiset(expected.texts)
    act_texts = _fold_multiset(actual.texts)
    jac = multiset_jaccard(exp_texts, act_texts)
    align = widget_align_rate(expected.widgets, actual.widgets,
                              cfg.widget_text_sim_min, page_texts=act_texts)
    value_mismatches = compare_keyed(expected.values, actual.values)
    list_mismatches = compare_keyed(expected.list_counts, actual.list_counts)

    detail: dict = {
        "jaccard": round(jac, 4),
        "widget_align_rate": round(align, 4),
        "value_mismatches": value_mismatches,
        "list_mismatches": list_mismatches,
    }

    # ---- L3 视觉参考（仅记录，不判失败） ------------------------------------
    if cfg.enable_l3:
        d = _l3_phash_distance(expected.screenshot, actual.screenshot)
        if d is not None:
            detail["l3_phash_distance"] = d

    l2_ok = (jac >= cfg.jaccard_min
             and align >= cfg.widget_align_min
             and not value_mismatches
             and not list_mismatches)
    if not l2_ok:
        detail["missing_texts"] = _multiset_diff(exp_texts, act_texts)[:20]
        detail["extra_texts"] = _multiset_diff(act_texts, exp_texts)[:20]
        return OracleVerdict(passed=False, kind="L2_CONTENT", detail=detail)

    return OracleVerdict(passed=True, kind=None, detail=detail)


def _multiset_diff(a: dict[str, int], b: dict[str, int]) -> list[str]:
    """a 中相对 b 缺失/超出的文本项。"""
    out = []
    for k, n in a.items():
        extra = n - b.get(k, 0)
        if extra > 0:
            out.append(k if extra == 1 else f"{k} (x{extra})")
    return sorted(out)


def _l3_phash_distance(shot_a: str, shot_h: str) -> Optional[int]:
    if not shot_a or not shot_h or not os.path.exists(shot_a) or not os.path.exists(shot_h):
        return None
    try:
        import imagehash
        from PIL import Image
        return int(imagehash.phash(Image.open(shot_a)) - imagehash.phash(Image.open(shot_h)))
    except Exception:
        return None

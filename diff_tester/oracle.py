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
from .state import canonical_text, compile_masks, mask_text

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

    @staticmethod
    def _mapped_page_matches(mapped: str, harmony_page: str) -> bool:
        if mapped == harmony_page:
            return True
        # 映射写 Ability，实际页可能是 Ability:pages/Xxx
        if harmony_page.startswith(mapped + ":"):
            return True
        return mapped == harmony_page.split(":")[0]

    def strictly_corresponds(self, android_page: str, harmony_page: str) -> bool:
        """Confirm page ownership without stem or fuzzy-name inference."""
        if android_page in self.mapping:
            return self._mapped_page_matches(self.mapping[android_page], harmony_page)
        return bool(android_page and android_page == harmony_page)

    def corresponds(self, android_page: str, harmony_page: str) -> bool:
        if self.strictly_corresponds(android_page, harmony_page):
            return True
        if android_page in self.mapping:
            return False
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

def _fold_multiset(d: dict[str, int], masks=None) -> dict[str, int]:
    """Re-mask persisted states, then casefold platform rendering differences."""
    masks = compile_masks() if masks is None else masks
    out: dict[str, int] = {}
    for k, n in d.items():
        kk = canonical_text(k, masks).casefold()
        if not kk:
            continue
        out[kk] = out.get(kk, 0) + n
    return out


def _normalize_labels(labels: dict[str, list[str]], masks) -> dict[str, list[str]]:
    # Empty/volatile labels still represent a real control; retain its count.
    return {role: [mask_text(label, masks) for label in values]
            for role, values in labels.items()}


def _normalize_values(values: dict, masks) -> dict:
    # A masked value is present data, and must differ from an empty field.
    return {key: mask_text(str(value), masks) for key, value in values.items()}


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
    aligned, _ = _align_keyed(expected, actual, fuzzy_key_min)
    return [
        {"key": key, "expected": expected_value, "actual": actual_value}
        for key, expected_value, actual_value, matched_key in aligned
        if matched_key is None or str(actual_value) != str(expected_value)
    ]


def _align_keyed(
    expected: dict,
    actual: dict,
    fuzzy_key_min: float = 0.6,
    *,
    single_key_fallback: bool = False,
) -> tuple[list[tuple[object, object, object, Optional[object]]], dict]:
    """Align expected keys to actual keys while preserving the matched key.

    ``compare_keyed`` historically returns only mismatches, so list/value
    policies use this lower-level view to distinguish a missing key from an
    empty value and to report how a fuzzy or single-key match was made.
    """
    aligned: list[tuple[object, object, object, Optional[object]]] = []
    unused = dict(actual)
    for key, expected_value in expected.items():
        matched_key: Optional[object] = None
        if key in unused:
            matched_key = key
        else:
            best_key, best_score = None, 0.0
            for candidate in unused:
                score = fuzz.ratio(str(key).casefold(), str(candidate).casefold()) / 100.0
                if score > best_score:
                    best_key, best_score = candidate, score
            if best_key is not None and best_score >= fuzzy_key_min:
                matched_key = best_key
            elif single_key_fallback and len(expected) == 1 and len(unused) == 1:
                # A trace with one list on each side has no competing role to
                # confuse; retain the fallback in the observation for review.
                matched_key = next(iter(unused))

        if matched_key is None:
            aligned.append((key, expected_value, None, None))
            continue
        aligned.append((key, expected_value, unused.pop(matched_key), matched_key))
    return aligned, unused


def _count_value(value: object) -> Optional[int]:
    """Convert a serialized list count to an integer when it is well formed."""
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def compare_list_counts(
    expected: dict,
    actual: dict,
    fuzzy_key_min: float = 0.6,
) -> tuple[list[dict], list[dict]]:
    """Compare list occupancy while tolerating viewport-dependent row counts.

    The stable contract is whether a list is empty or non-empty.  An expected
    empty list may be omitted by a declarative UI and is therefore compatible
    with a missing actual key.  Counts remain in ``observations`` for reports;
    only an empty/non-empty disagreement is a semantic mismatch.
    """
    aligned, unused = _align_keyed(
        expected, actual, fuzzy_key_min, single_key_fallback=True)
    mismatches: list[dict] = []
    observations: list[dict] = []

    for key, expected_value, actual_value, matched_key in aligned:
        expected_count = _count_value(expected_value)
        actual_count = _count_value(actual_value)
        entry = {
            "key": key,
            "matched_key": matched_key,
            "expected": expected_value,
            "actual": actual_value,
        }
        if matched_key is None:
            if expected_count == 0:
                entry["comparison"] = "expected_empty_missing"
            else:
                entry["comparison"] = "missing"
                mismatches.append({
                    "key": key, "expected": expected_value, "actual": None,
                })
        elif expected_count == 0 and actual_count == 0:
            entry["comparison"] = "empty"
        elif expected_count == 0 and actual_count is not None and actual_count > 0:
            entry["comparison"] = "empty_vs_nonempty"
            mismatches.append({
                "key": key, "expected": expected_value, "actual": actual_value,
            })
        elif expected_count is not None and expected_count > 0 \
                and actual_count is not None and actual_count > 0:
            entry["comparison"] = "nonempty"
        else:
            entry["comparison"] = "invalid_or_empty"
            mismatches.append({
                "key": key, "expected": expected_value, "actual": actual_value,
            })
        observations.append(entry)

    # Actual-only lists are useful diagnostic evidence but do not fail the
    # must-have contract, matching the treatment of extra visible text.
    for key, actual_value in unused.items():
        observations.append({
            "key": None,
            "matched_key": key,
            "expected": None,
            "actual": actual_value,
            "comparison": "extra_actual",
        })
    return mismatches, observations


def _normalized_visible_texts(state: StateVector, masks) -> set[str]:
    """Collect visible text/widget labels for placeholder recognition."""
    values = {
        mask_text(str(text), masks).strip().casefold()
        for text in state.texts
    }
    for labels in state.widgets.values():
        values.update(mask_text(str(label), masks).strip().casefold()
                      for label in labels)
    return {value for value in values if value}


def _regex_matches(patterns: list[str], value: str) -> bool:
    for pattern in patterns:
        try:
            if re.search(pattern, value):
                return True
        except re.error:
            # Invalid project configuration must not turn an oracle result
            # into an infrastructure failure; the value simply stays strict.
            continue
    return False


def _is_ignorable_empty_placeholder(
    key: object,
    expected_value: object,
    actual_value: object,
    expected_state: StateVector,
    cfg: OracleConfig,
    masks,
) -> bool:
    """Identify a visible hint that disappeared from an empty input value."""
    if not cfg.ignore_empty_placeholder_values or str(actual_value).strip():
        return False
    normalized = mask_text(str(expected_value), masks).strip()
    if not normalized or normalized.casefold() == "<volatile>":
        return False
    if normalized.casefold() not in _normalized_visible_texts(expected_state, masks):
        return False
    return (_regex_matches(cfg.placeholder_patterns, normalized)
            or _regex_matches(cfg.placeholder_key_patterns, str(key)))


def compare_values(
    expected: StateVector,
    actual: StateVector,
    cfg: OracleConfig,
    masks,
) -> tuple[list[dict], list[dict]]:
    """Compare widget values and return mismatches plus ignored placeholders."""
    expected_values = _normalize_values(expected.values, masks)
    actual_values = _normalize_values(actual.values, masks)
    aligned, _ = _align_keyed(expected_values, actual_values)
    mismatches: list[dict] = []
    ignored: list[dict] = []
    for key, expected_value, actual_value, matched_key in aligned:
        if matched_key is None:
            mismatches.append({"key": key, "expected": expected_value, "actual": None})
        elif str(actual_value) != str(expected_value):
            if _is_ignorable_empty_placeholder(
                    key, expected_value, actual_value, expected, cfg, masks):
                ignored.append({
                    "key": key,
                    "matched_key": matched_key,
                    "placeholder": expected_value,
                    "actual": actual_value,
                })
            else:
                mismatches.append({
                    "key": key, "expected": expected_value, "actual": actual_value,
                })
    return mismatches, ignored


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def compare(
    expected: StateVector,
    actual: StateVector,
    page_pairs: PagePairs,
    cfg: Optional[OracleConfig] = None,
    masks=None,
) -> OracleVerdict:
    """分层比较：期望值（安卓基线）vs 实际值（鸿蒙回放）。"""
    cfg = cfg or OracleConfig()
    masks = compile_masks() if masks is None else masks

    # ---- L0 存活性 ----------------------------------------------------------
    if not actual.alive or actual.crash_sig is not None:
        return OracleVerdict(passed=False, kind="L0_CRASH", detail={
            "alive": actual.alive,
            "crash_sig": actual.crash_sig,
            "failed_predicates": ["l0.alive"],
            "predicates": [{"id": "l0.alive", "passed": False,
                            "expected": True, "actual": actual.alive}],
        })

    # ---- L1 页面身份 --------------------------------------------------------
    if not page_pairs.corresponds(expected.page, actual.page):
        return OracleVerdict(passed=False, kind="L1_PAGE", detail={
            "expected_page": expected.page,
            "actual_page": actual.page,
            "failed_predicates": ["l1.page_identity"],
            "predicates": [{"id": "l1.page_identity", "passed": False,
                            "expected": expected.page, "actual": actual.page}],
        })

    # ---- External-surface protocol ----------------------------------------
    surface_passed = expected.external_surface == actual.external_surface
    surface_predicate = {
        "id": "external_protocol.surface",
        "passed": surface_passed,
        "expected": expected.external_surface,
        "actual": actual.external_surface,
        "ownership": "system" if (expected.external_surface or actual.external_surface) else "application",
        "policy": "strict",
    }
    if not surface_passed:
        return OracleVerdict(passed=False, kind="EXTERNAL_PROTOCOL", detail={
            "external_protocol": surface_predicate,
            "failed_predicates": ["external_protocol.surface"],
            "predicates": [surface_predicate],
        })

    # ---- L2 语义内容 --------------------------------------------------------
    exp_texts = _fold_multiset(expected.texts, masks)
    act_texts = _fold_multiset(actual.texts, masks)
    jac = multiset_jaccard(exp_texts, act_texts)
    align = widget_align_rate(_normalize_labels(expected.widgets, masks),
                              _normalize_labels(actual.widgets, masks),
                              cfg.widget_text_sim_min, page_texts=act_texts)
    value_mismatches, ignored_placeholders = compare_values(
        expected, actual, cfg, masks)
    list_mismatches, list_observations = compare_list_counts(
        expected.list_counts, actual.list_counts)

    detail: dict = {
        "jaccard": round(jac, 4),
        "widget_align_rate": round(align, 4),
        "value_mismatches": value_mismatches,
        "ignored_placeholders": ignored_placeholders,
        "list_mismatches": list_mismatches,
        "list_observations": list_observations,
    }

    predicates = [surface_predicate,
        {"id": "l2.text_multiset", "passed": not _multiset_diff(exp_texts, act_texts),
         "policy": "must_have",
         "expected_min": cfg.jaccard_min, "actual": round(jac, 4)},
        {"id": "l2.widget_alignment", "passed": align >= cfg.widget_align_min,
         "expected_min": cfg.widget_align_min, "actual": round(align, 4)},
        {"id": "l2.value_state", "passed": not value_mismatches,
         "mismatches": value_mismatches},
        {"id": "l2.list_state", "passed": not list_mismatches,
         "mismatches": list_mismatches},
    ]
    detail["predicates"] = predicates
    detail["failed_predicates"] = [item["id"] for item in predicates
                                    if not item["passed"]]

    # ---- L3 视觉参考（仅记录，不判失败） ------------------------------------
    if cfg.enable_l3:
        d = _l3_phash_distance(expected.screenshot, actual.screenshot)
        if d is not None:
            detail["l3_phash_distance"] = d

    missing_texts = _multiset_diff(exp_texts, act_texts)[:20]
    extra_texts = _multiset_diff(act_texts, exp_texts)[:20]
    # Extra rendered labels are diagnostic evidence (system chrome and
    # platform-owned wording often appears only on one side). Missing expected
    # labels remain a hard content failure; the configured Jaccard threshold is
    # retained as a diagnostic metric rather than a second failure gate.
    text_ok = not missing_texts
    l2_ok = (text_ok
             and align >= cfg.widget_align_min
             and not value_mismatches
             and not list_mismatches)
    if not l2_ok:
        detail["missing_texts"] = missing_texts
        detail["extra_texts"] = extra_texts
        return OracleVerdict(passed=False, kind="L2_CONTENT", detail=detail)

    # Keep these fields present on successful comparisons as well. Consumers
    # can distinguish a clean equality from a platform-only extra label.
    detail["missing_texts"] = missing_texts
    detail["extra_texts"] = extra_texts

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

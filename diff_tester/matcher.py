"""控件对齐器（§6.2）——整个方案的技术核心。

match(fp, tree, action) 在目标端控件树中搜索与录制指纹最匹配的控件：
  score = w_id*sim_id + w_text*sim_text + w_role*sim_role + w_pos*sim_pos + w_img*sim_img
某分量缺失时其权重按比例摊给其余分量。
"""
from __future__ import annotations

import math
import os
from typing import Optional, Union

from rapidfuzz import fuzz

from .config import MatcherConfig
from .schemas import MatchResult, TargetFingerprint, UNode

#: 兼容角色对（sim_role = 0.5）
_COMPATIBLE_ROLES: set[frozenset[str]] = {
    frozenset(("button", "image")),      # 图标按钮两端角色易漂移
    frozenset(("button", "text")),       # 可点击文本 vs 按钮
    frozenset(("button", "listitem")),
    frozenset(("text", "listitem")),
    frozenset(("checkbox", "switch")),
    frozenset(("list", "container")),
}


def _roles_compatible(a: str, b: str) -> bool:
    return frozenset((a, b)) in _COMPATIBLE_ROLES


def _sim_text(fp: TargetFingerprint, c: UNode) -> Optional[float]:
    a = fp.text or fp.desc
    b = c.text or c.desc
    if not a:
        return None            # 指纹无文本 → 分量缺失，权重转移
    return fuzz.ratio(a, b) / 100.0


def _sim_id(fp: TargetFingerprint, c: UNode) -> Optional[float]:
    if not fp.id_hint or not c.id:
        return None            # 任一端无 id → 缺失，权重转移
    return fuzz.ratio(fp.id_hint, c.id) / 100.0


def _sim_role(fp: TargetFingerprint, c: UNode) -> float:
    if fp.role == c.role:
        return 1.0
    if _roles_compatible(fp.role, c.role):
        return 0.5
    return 0.0


def _sim_pos(fp: TargetFingerprint, c: UNode) -> float:
    fx = (fp.rel_bounds[0] + fp.rel_bounds[2]) / 2.0
    fy = (fp.rel_bounds[1] + fp.rel_bounds[3]) / 2.0
    cx, cy = c.center_rel()
    dist = math.hypot(fx - cx, fy - cy)
    base = 1.0 - min(1.0, dist / 0.5)

    fa = max(1e-6, (fp.rel_bounds[2] - fp.rel_bounds[0]) * (fp.rel_bounds[3] - fp.rel_bounds[1]))
    ca = max(1e-6, c.rel_area())
    area_penalty = min(fa, ca) / max(fa, ca)
    return base * area_penalty


def _sim_img(fp: TargetFingerprint, c: UNode,
             screen_image: Union[str, "object", None]) -> Optional[float]:
    """感知哈希相似度：录制 patch vs 候选控件区域截图。失败/缺件 → None（权重转移）。"""
    if not fp.patch_path or not os.path.exists(fp.patch_path) or screen_image is None:
        return None
    try:
        import imagehash
        from PIL import Image

        img = Image.open(screen_image) if isinstance(screen_image, str) else screen_image
        x1, y1, x2, y2 = c.abs_bounds
        if x2 <= x1 or y2 <= y1:
            return None
        crop = img.crop((x1, y1, x2, y2))
        d = imagehash.phash(Image.open(fp.patch_path)) - imagehash.phash(crop)
        return max(0.0, 1.0 - d / 64.0)
    except Exception:
        return None


def _candidates_for(tree: UNode, action: str) -> list[UNode]:
    inter = list(tree.iter_interactive())
    if action in ("CLICK", "LONG_CLICK"):
        return [n for n in inter if n.clickable]
    if action == "TYPE":
        return [n for n in inter if n.editable]
    return inter


def match(
    fp: TargetFingerprint,
    tree: UNode,
    action: str,
    cfg: Optional[MatcherConfig] = None,
    screen_image: Union[str, "object", None] = None,
) -> MatchResult:
    """在目标端控件树中为指纹 fp 寻找最匹配候选。

    screen_image：目标端当前整屏截图（路径或 PIL Image），供 sim_img 裁剪候选区域。
    """
    cfg = cfg or MatcherConfig()
    cands = _candidates_for(tree, action)
    if not cands:
        return MatchResult(kind="UNMAPPED", node=None, score=0.0,
                           detail={"reason": "no_candidates", "action": action})

    weights = {"id": cfg.w_id, "text": cfg.w_text, "role": cfg.w_role,
               "pos": cfg.w_pos, "img": cfg.w_img}
    scored: list[tuple[float, UNode, dict]] = []
    for c in cands:
        comps: dict[str, Optional[float]] = {
            "id": _sim_id(fp, c),
            "text": _sim_text(fp, c),
            "role": _sim_role(fp, c),
            "pos": _sim_pos(fp, c),
            "img": _sim_img(fp, c, screen_image),
        }
        present = {k: v for k, v in comps.items() if v is not None}
        total_w = sum(weights[k] for k in present)
        score = sum(weights[k] * v for k, v in present.items()) / total_w if total_w > 0 else 0.0
        scored.append((score, c, {k: (round(v, 4) if v is not None else None)
                                  for k, v in comps.items()}))

    scored.sort(key=lambda t: t[0], reverse=True)
    top_detail = [
        {"id": c.id, "text": c.text, "role": c.role, "score": round(s, 4), "components": comp}
        for s, c, comp in scored[:5]
    ]
    top1_score, top1_node, _ = scored[0]
    top2_score = scored[1][0] if len(scored) > 1 else 0.0

    if top1_score >= cfg.th_hi and (top1_score - top2_score) >= cfg.min_gap:
        kind = "MATCHED"
    elif top1_score >= cfg.th_lo:
        kind = "AMBIGUOUS"     # M5 后可接 LLM 仲裁
    else:
        kind = "UNMAPPED"

    return MatchResult(
        kind=kind,
        node=top1_node if kind != "UNMAPPED" else None,
        score=top1_score,
        second_score=top2_score,
        detail={"top": top_detail},
    )

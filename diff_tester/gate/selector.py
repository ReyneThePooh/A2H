"""增量轨迹选择（设计文档 §6.5 / AI实现参考2 §5）。

Unit → 页面集合来自 workspace/unit_page_map.json（由流水线侧从
translation_plan.json 推导）；轨迹途经页面 = 各事件 post_state.page 去重。

页面名比较用词干归一（复用 oracle.PagePairs 的词干化），消除
`.MainActivity` / `MainActivity` / `com.x.MainActivity` 的书写差异。

保守原则：changed_units 含 __global__、Unit 映射缺失、或页面通配 "*"
→ 全量回放（种子总量 ≤ 10 条，全量代价可接受）。
"""
from __future__ import annotations

from typing import Iterable

from ..oracle import PagePairs
from ..schemas import Trace

#: 流水线侧约定：改动涉及全局文件（Application/公共 utils）时传该 Unit 名
GLOBAL_UNIT = "__global__"


def trace_pages(trace: Trace) -> set[str]:
    """轨迹途经的安卓页面集合（各事件 post_state.page 去重）。"""
    return {ev.post_state.page for ev in trace.events
            if ev.post_state and ev.post_state.page}


def _stems(pages: Iterable[str]) -> set[str]:
    return {PagePairs._stem(p) for p in pages if p} - {""}


def select_traces(
    traces: list[Trace],
    changed_units: list[str],
    unit_page_map: dict[str, dict],
    full_replay: bool = False,
) -> tuple[list[Trace], list[Trace]]:
    """按改动 Unit 选择需回放的轨迹，返回 (selected, skipped)。"""
    def _all() -> tuple[list[Trace], list[Trace]]:
        return list(traces), []

    if full_replay or not changed_units or GLOBAL_UNIT in changed_units:
        return _all()

    target_pages: set[str] = set()
    for unit in changed_units:
        entry = unit_page_map.get(unit)
        if entry is None:                      # 映射缺失 → 保守全量
            return _all()
        pages = list(entry.get("android_pages", []))
        if "*" in pages:
            return _all()
        target_pages |= _stems(pages)

    if not target_pages:                       # 改动 Unit 不含任何页面 → 保守全量
        return _all()

    selected: list[Trace] = []
    skipped: list[Trace] = []
    for t in traces:
        if _stems(trace_pages(t)) & target_pages:
            selected.append(t)
        else:
            skipped.append(t)
    return selected, skipped

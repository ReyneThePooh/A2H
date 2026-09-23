"""增量轨迹选择（设计文档 §6.5 / AI实现参考2 §5）。

Unit → 页面集合来自 workspace/unit_page_map.json（由流水线侧从
translation_plan.json 推导）；轨迹途经页面 = 各事件 post_state.page 去重。

页面名比较用词干归一（复用 oracle.PagePairs 的词干化），消除
`.MainActivity` / `MainActivity` / `com.x.MainActivity` 的书写差异。

保守原则：changed_units 含 __global__、Unit 映射缺失、或页面通配 "*"
→ 全量回放（种子总量 ≤ 10 条，全量代价可接受）。
"""
from __future__ import annotations

from typing import Iterable, Optional

from ..oracle import PagePairs
from ..schemas import Trace

#: 流水线侧约定：改动涉及全局文件（Application/公共 utils）时传该 Unit 名
GLOBAL_UNIT = "__global__"


def trace_pages(trace: Trace) -> set[str]:
    """轨迹途经的安卓页面集合（各事件 post_state.page 去重）。"""
    states = [trace.initial_state]
    states.extend(s for ev in trace.events for s in (ev.pre_state, ev.post_state))
    return {state.page for state in states if state and state.page}


def trace_step_count(trace: Trace) -> int:
    """Return the number of planned actions in a trace.

    Keeping this as a small helper makes the short-seed policy independent of
    the replay implementation.  A trace's step count is its event count; a
    recorded step number is evidence, not the source of truth for filtering.
    """
    return len(trace.events)


def _validate_max_steps(max_steps: Optional[int]) -> None:
    if max_steps is None:
        return
    if type(max_steps) is not int or max_steps < 0:
        raise ValueError("max_steps must be a non-negative integer or None")


def filter_traces_by_max_steps(
    traces: Iterable[Trace],
    max_steps: Optional[int] = None,
) -> tuple[list[Trace], list[Trace]]:
    """Split traces into those within and above an optional step limit.

    The input order is preserved.  With no limit this is an explicit no-op,
    which lets callers add the option without changing existing gate runs.
    """
    _validate_max_steps(max_steps)
    all_traces = list(traces)
    if max_steps is None:
        return all_traces, []
    eligible = [trace for trace in all_traces
                if trace_step_count(trace) <= max_steps]
    skipped = [trace for trace in all_traces
               if trace_step_count(trace) > max_steps]
    return eligible, skipped


def select_shortest_cover(
    traces: Iterable[Trace],
    max_traces: Optional[int] = None,
    max_steps: Optional[int] = None,
) -> tuple[list[Trace], list[Trace]]:
    """Greedily select a shortest trace set covering all visited pages.

    Each iteration maximizes newly covered pages, then minimizes event count,
    then uses ``trace_id`` for deterministic ties.  ``max_steps`` excludes
    long traces before coverage is calculated, while ``max_traces`` limits the
    number of selected traces.  The returned second list contains every trace
    not selected, including traces excluded by either limit.

    This helper is intentionally separate from :func:`select_traces`: the
    latter answers the gate's changed-Unit query, whereas this function is an
    offline short-seed policy.
    """
    if max_traces is not None and (
        type(max_traces) is not int or max_traces < 0
    ):
        raise ValueError("max_traces must be a non-negative integer or None")
    all_traces = list(traces)
    eligible, _ = filter_traces_by_max_steps(all_traces, max_steps)
    if max_traces == 0 or not eligible:
        return [], list(all_traces)

    remaining = list(eligible)
    all_pages = set().union(*(trace_pages(trace) for trace in eligible))
    covered: set[str] = set()
    selected: list[Trace] = []

    while remaining and (max_traces is None or len(selected) < max_traces):
        candidate = min(
            remaining,
            key=lambda trace: (
                -len(_stems(trace_pages(trace)) - covered),
                trace_step_count(trace),
                trace.trace_id,
            ),
        )
        gain = _stems(trace_pages(candidate)) - covered
        if not gain and covered:
            break
        selected.append(candidate)
        remaining.remove(candidate)
        covered.update(_stems(trace_pages(candidate)))
        if covered >= _stems(all_pages):
            break

    selected_ids = {id(trace) for trace in selected}
    skipped = [trace for trace in all_traces if id(trace) not in selected_ids]
    return selected, skipped


def _stems(pages: Iterable[str]) -> set[str]:
    return {PagePairs._stem(p) for p in pages if p} - {""}


def select_traces(
    traces: list[Trace],
    changed_units: list[str],
    unit_page_map: dict[str, dict],
    full_replay: bool = False,
    max_steps: Optional[int] = None,
) -> tuple[list[Trace], list[Trace]]:
    """按改动 Unit 选择需回放的轨迹，返回 (selected, skipped)。

    ``max_steps`` is opt-in and only filters traces longer than the supplied
    limit.  Existing callers that omit it retain the original selection
    behavior, including conservative full-replay fallbacks.
    """
    _validate_max_steps(max_steps)

    def _all() -> tuple[list[Trace], list[Trace]]:
        return filter_traces_by_max_steps(traces, max_steps)

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
        if (max_steps is None or trace_step_count(t) <= max_steps) \
                and _stems(trace_pages(t)) & target_pages:
            selected.append(t)
        else:
            skipped.append(t)
    return selected, skipped

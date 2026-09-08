"""FLAKY（不稳定轨迹）观察名单管理（AI实现参考2 §5）。

分叉但复跑不复现 → 本轮放行、记一次 FLAKY；连续 ESCALATE_AFTER 轮
FLAKY → 升级为失败。轨迹通过时清零计数。

状态持久化在 workspace/flaky.json，跨门禁轮次保留。
"""
from __future__ import annotations

import os

from ..schemas import load_json, save_json

#: 连续 FLAKY 达该轮数后升级为失败
ESCALATE_AFTER = 2


class FlakyList:
    def __init__(self, path: str):
        self.path = path
        self.counts: dict[str, int] = {}
        if os.path.exists(path):
            self.counts = {k: int(v) for k, v in load_json(path).items()}

    def mark(self, trace_id: str) -> int:
        """记一次 FLAKY，返回该轨迹当前连续 FLAKY 次数。"""
        self.counts[trace_id] = self.counts.get(trace_id, 0) + 1
        return self.counts[trace_id]

    def clear(self, trace_id: str) -> None:
        """轨迹本轮通过（或分叉被确认）→ 连续计数清零。"""
        self.counts.pop(trace_id, None)

    def escalated(self, trace_id: str) -> bool:
        return self.counts.get(trace_id, 0) >= ESCALATE_AFTER

    def save(self) -> None:
        save_json(self.counts, self.path)

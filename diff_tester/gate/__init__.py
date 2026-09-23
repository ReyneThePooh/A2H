"""轻量化环内差分测试门禁（《AI实现参考2_A2H迭代轻量测试》）。

复用完整版模块（adapters / normalize / matcher / oracle / state / replayer /
schemas），只做子集编排：固定种子轨迹回放 + L0–L2 预言 + 增量选择 + FLAKY 管理。

与 A2H 流水线的契约（§3）：
- 流水线侧在 workspace 下提供 unit_page_map.json、page_pairs.json、seeds/；
- 本包不 import 流水线内部模块（隔离性验收 §9）。
"""
from .flaky import FlakyList
from .gate_runner import GateRequest, GateResult, run_gate
from .repair_report import (RepairReport, build_repair_report,
                            build_repair_reports, cluster_reports)
from .selector import (
    GLOBAL_UNIT,
    filter_traces_by_max_steps,
    select_shortest_cover,
    select_traces,
    trace_pages,
    trace_step_count,
)

__all__ = [
    "FlakyList",
    "GateRequest",
    "GateResult",
    "run_gate",
    "RepairReport",
    "build_repair_report",
    "build_repair_reports",
    "cluster_reports",
    "GLOBAL_UNIT",
    "filter_traces_by_max_steps",
    "select_shortest_cover",
    "select_traces",
    "trace_pages",
    "trace_step_count",
]

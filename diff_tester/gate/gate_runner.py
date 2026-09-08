"""单轮门禁执行器（AI实现参考2 §3/§5，主入口 run_gate）。

流程：加载种子/契约文件 → 增量选择 → 部署 HAP → 逐轨迹清数据回放
（oracle 只启 L0–L2）→ 分叉复跑 1 次确认 → FLAKY 管理 → 历史落盘。

与 A2H 流水线的隔离（验收 §9）：本模块只消费 workspace 下的
unit_page_map.json / page_pairs.json / seeds/，不 import 流水线内部模块。

可测试性：run_gate 允许注入 harmony 适配器与 replay_fn，
离线单测无需真机即可覆盖选择/复跑/FLAKY/落盘逻辑。
"""
from __future__ import annotations

import glob
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..config import Config
from ..oracle import PagePairs
from ..schemas import Trace, TraceResult, load_json, save_json
from .flaky import ESCALATE_AFTER, FlakyList
from .repair_report import RepairReport, build_repair_report, build_timeout_report
from .selector import select_traces

logger = logging.getLogger("diff_tester")

#: replay 的可注入签名：(trace, harmony, page_pairs, cfg, results_root) -> TraceResult
ReplayFn = Callable[..., TraceResult]


@dataclass
class GateRequest:
    """门禁调用参数（A2H 侧组装）。"""

    bundle: str                              # 鸿蒙应用 bundleName
    workspace: str                           # 门禁工作目录（种子/映射/历史）
    changed_units: list[str] = field(default_factory=list)  # 本轮改动的 Unit 名
    hap_path: Optional[str] = None           # 提供则先 hdc install
    harmony_device: Optional[str] = None     # hdc 序列号（单设备可省略）
    hdc_path: Optional[str] = None           # hdc 可执行路径（默认 PATH 中的 hdc）
    full_replay: bool = False                # True=忽略增量、全量回放（出厂检查）
    seeds_dir: Optional[str] = None          # 默认 workspace/seeds
    config_yaml: Optional[str] = None        # diff_tester config.yaml 覆盖
    time_budget_s: float = 300.0             # 全轮硬上限（§5 预算控制）


@dataclass
class GateResult:
    passed: bool
    ran_traces: list[str]                    # 本轮实际回放的轨迹 id
    skipped_traces: list[str]                # 增量策略跳过的轨迹 id
    reports: list[RepairReport]              # 失败时非空
    flaky: list[str]                         # 本轮标记为 FLAKY 的轨迹
    elapsed_s: float
    round_no: int = 0
    trace_passed: dict[str, bool] = field(default_factory=dict)

    @property
    def consistency_rate(self) -> float:
        """本轮一致率：通过轨迹 / 实际回放轨迹（历史趋势曲线用）。"""
        if not self.trace_passed:
            return 1.0
        return sum(self.trace_passed.values()) / len(self.trace_passed)

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "round_no": self.round_no,
            "ran_traces": list(self.ran_traces),
            "skipped_traces": list(self.skipped_traces),
            "reports": [r.to_dict() for r in self.reports],
            "flaky": list(self.flaky),
            "elapsed_s": round(self.elapsed_s, 1),
            "trace_passed": dict(self.trace_passed),
            "consistency_rate": round(self.consistency_rate, 4),
        }


def load_seeds(seeds_dir: str) -> list[Trace]:
    files = sorted(glob.glob(os.path.join(seeds_dir, "*.json")))
    return [Trace.from_dict(load_json(f)) for f in files]


def _next_round_no(history_dir: str) -> int:
    existing = glob.glob(os.path.join(history_dir, "round_*.json"))
    return len(existing) + 1


def run_gate(
    req: GateRequest,
    harmony=None,
    replay_fn: Optional[ReplayFn] = None,
) -> GateResult:
    """执行一轮门禁，返回 GateResult。

    Args:
        req: 门禁请求（契约文件位于 req.workspace 下）
        harmony: 可注入的鸿蒙适配器（默认按 req 创建 HarmonyAdapter）
        replay_fn: 可注入的回放函数（默认 replayer.replay）
    """
    t0 = time.monotonic()
    os.makedirs(req.workspace, exist_ok=True)

    # ---- 配置：环内只保留 L0–L2 预言 ----------------------------------------
    cfg = Config.load(req.config_yaml)
    cfg.oracle.enable_l3 = False
    if req.hdc_path:
        cfg.device.hdc_path = req.hdc_path
    if req.harmony_device:
        cfg.device.harmony_serial = req.harmony_device

    # ---- 加载种子与契约文件 --------------------------------------------------
    seeds_dir = req.seeds_dir or os.path.join(req.workspace, "seeds")
    traces = load_seeds(seeds_dir)
    if not traces:
        raise FileNotFoundError(f"种子轨迹目录为空: {seeds_dir}")

    upm_path = os.path.join(req.workspace, "unit_page_map.json")
    unit_page_map = load_json(upm_path) if os.path.exists(upm_path) else {}

    pp_path = os.path.join(req.workspace, "page_pairs.json")
    page_pairs = PagePairs.load(
        pp_path if os.path.exists(pp_path) else None,
        stem_sim_min=cfg.oracle.page_stem_sim_min,
    )

    # ---- 增量选择 -------------------------------------------------------------
    selected, skipped = select_traces(
        traces, req.changed_units, unit_page_map, req.full_replay
    )
    logger.info("[gate] 增量选择: 回放 %d 条 / 跳过 %d 条 (changed_units=%s)",
                len(selected), len(skipped), req.changed_units)

    history_dir = os.path.join(req.workspace, "history")
    round_no = _next_round_no(history_dir)
    runs_root = os.path.join(req.workspace, "runs", f"round_{round_no:03d}")

    # ---- 部署 -----------------------------------------------------------------
    if harmony is None:
        from ..adapters.harmony import HarmonyAdapter
        harmony = HarmonyAdapter(cfg, bundle=req.bundle,
                                 serial=cfg.device.harmony_serial)
    if req.hap_path:
        harmony.install(req.hap_path)
    harmony.ensure_ready()

    if replay_fn is None:
        from ..replayer import replay as replay_fn  # type: ignore[no-redef]

    # ---- 逐轨迹回放 + 复跑确认 + FLAKY 管理 -----------------------------------
    flaky_list = FlakyList(os.path.join(req.workspace, "flaky.json"))
    reports: list[RepairReport] = []
    flaky_now: list[str] = []
    ran: list[str] = []
    trace_passed: dict[str, bool] = {}

    for trace in selected:
        elapsed = time.monotonic() - t0
        if elapsed >= req.time_budget_s:
            pending = [t.trace_id for t in selected if t.trace_id not in set(ran)]
            logger.warning("[gate] 超出全轮预算 %.0fs，剩余 %d 条未回放",
                           req.time_budget_s, len(pending))
            reports.append(build_timeout_report(pending, elapsed))
            break

        tid = trace.trace_id
        ran.append(tid)
        result = replay_fn(trace, harmony, page_pairs, cfg, runs_root)
        save_json(result.to_dict(), os.path.join(runs_root, tid, "result.json"))

        if result.passed:
            flaky_list.clear(tid)
            trace_passed[tid] = True
            continue

        # 分叉 → 立即复跑一次：同步骤同 kind 复现才确认（§6.6 判定协议）
        confirm = replay_fn(trace, harmony, page_pairs, cfg,
                            os.path.join(runs_root, "confirm"))
        reproduced = (
            not confirm.passed
            and confirm.divergence is not None
            and result.divergence is not None
            and confirm.divergence.diverged_step == result.divergence.diverged_step
            and confirm.divergence.kind == result.divergence.kind
        )
        if reproduced:
            flaky_list.clear(tid)
            trace_passed[tid] = False
            reports.append(build_repair_report(trace, result, unit_page_map))
            logger.warning("[gate] %s 分叉复现（step=%s kind=%s）→ 生成修复报告",
                           tid, result.divergence.diverged_step,
                           result.divergence.kind)
        else:
            n = flaky_list.mark(tid)
            flaky_now.append(tid)
            if n >= ESCALATE_AFTER:
                rep = build_repair_report(trace, result, unit_page_map)
                rep.flaky_escalated = True
                reports.append(rep)
                trace_passed[tid] = False
                logger.warning("[gate] %s 连续 %d 轮 FLAKY → 升级为失败", tid, n)
            else:
                trace_passed[tid] = True   # 本轮放行，计入观察名单
                logger.warning("[gate] %s 分叉不复现 → FLAKY（第 %d 次）", tid, n)

    flaky_list.save()

    gate_result = GateResult(
        passed=not reports,
        ran_traces=ran,
        skipped_traces=[t.trace_id for t in skipped],
        reports=reports,
        flaky=flaky_now,
        elapsed_s=time.monotonic() - t0,
        round_no=round_no,
        trace_passed=trace_passed,
    )
    save_json(gate_result.to_dict(),
              os.path.join(history_dir, f"round_{round_no:03d}.json"))
    logger.info("[gate] 第 %d 轮门禁: %s（一致率 %.0f%%, 耗时 %.1fs）",
                round_no, "通过" if gate_result.passed else "未通过",
                gate_result.consistency_rate * 100, gate_result.elapsed_s)
    return gate_result

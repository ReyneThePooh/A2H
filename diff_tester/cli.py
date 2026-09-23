"""CLI 入口：record / replay / evaluate 三个子命令（§7）。

三个子命令可独立运行（录制一次、回放多次）。
所有配置项支持 --config config.yaml 覆盖默认值。

用法示例：
  python -m diff_tester record  --pkg com.example.app --device <adb_serial> \\
         --traces 20 --max-steps 30 --seeds seeds/ --out traces_out/
  python -m diff_tester replay  --bundle com.example.hm --device <hdc_serial> \\
         --traces traces_out/traces --page-pairs page_pairs.json --out results/
  python -m diff_tester evaluate --results results/ --confirm-runs 3 --out report/
"""
from __future__ import annotations

import argparse
import glob
import logging
import os
import shutil
import sys

from .config import Config
from .schemas import Trace, TraceResult, load_json, save_json

logger = logging.getLogger("diff_tester")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )


def _check_tool(path: str, name: str) -> None:
    if not (os.path.isfile(path) or shutil.which(path)):
        raise SystemExit(
            f"错误：找不到 {name}（{path}）。\n"
            f"请安装后在 config.yaml 的 device.{name}_path 中给出完整路径，"
            f"或将其加入 PATH。"
        )


def _load_traces(traces_dir: str) -> list[Trace]:
    files = sorted(glob.glob(os.path.join(traces_dir, "*.json")))
    if not files:
        raise SystemExit(f"错误：{traces_dir} 下没有轨迹 JSON 文件")
    return [Trace.from_dict(load_json(f)) for f in files]


# ---------------------------------------------------------------------------
# record
# ---------------------------------------------------------------------------

def cmd_record(args: argparse.Namespace) -> int:
    from .adapters.android import AndroidAdapter
    from .explorer import explore

    cfg = Config.load(args.config)
    _check_tool(cfg.device.adb_path, "adb")

    android = AndroidAdapter(cfg, pkg=args.pkg, serial=args.device)
    if args.apk:
        android.install(args.apk)
    android.ensure_ready()

    seeds = None
    if args.seeds:
        seeds = _load_traces(args.seeds)
        logger.info("加载 %d 条种子轨迹", len(seeds))

    paths = explore(
        android, cfg,
        out_dir=args.out,
        n_traces=args.traces,
        max_steps=args.max_steps,
        seeds=seeds,
        app_pkg_harmony=args.harmony_bundle or "",
    )
    logger.info("录制完成：%d 条轨迹 → %s", len(paths), os.path.join(args.out, "traces"))
    return 0


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------

def cmd_replay(args: argparse.Namespace) -> int:
    from .adapters.harmony import HarmonyAdapter
    from .oracle import PagePairs
    from .replayer import replay

    cfg = Config.load(args.config)
    _check_tool(cfg.device.hdc_path, "hdc")

    harmony = HarmonyAdapter(cfg, bundle=args.bundle, serial=args.device)
    if args.hap:
        harmony.install(args.hap)
    harmony.ensure_ready()

    page_pairs = PagePairs.load(args.page_pairs,
                                stem_sim_min=cfg.oracle.page_stem_sim_min)
    traces = _load_traces(args.traces)
    os.makedirs(args.out, exist_ok=True)

    n_pass = 0
    for trace in traces:
        result = replay(trace, harmony, page_pairs, cfg, args.out)
        save_json(result.to_dict(),
                  os.path.join(args.out, trace.trace_id, "result.json"))
        if result.passed and result.status == "PASS":
            n_pass += 1
    logger.info("回放完成：%d/%d 条轨迹通过 → %s", n_pass, len(traces), args.out)
    return 0 if traces and n_pass == len(traces) else 1


# ---------------------------------------------------------------------------
# evaluate
# ---------------------------------------------------------------------------

def cmd_evaluate(args: argparse.Namespace) -> int:
    from .attribution import attribute_all
    from .metrics import compute_metrics
    from .oracle import PagePairs
    from .report import write_reports

    cfg = Config.load(args.config)
    if args.whitelist:
        cfg.whitelist_path = args.whitelist

    result_files = sorted(glob.glob(os.path.join(args.results, "*", "result.json")))
    if not result_files:
        raise SystemExit(f"错误：{args.results} 下没有 result.json（先运行 replay）")
    results = [TraceResult.from_dict(load_json(f)) for f in result_files]
    logger.info("加载 %d 条回放结果", len(results))

    # 归因（可选：提供鸿蒙设备则做复跑确认，另提供安卓设备则排除源应用缺陷）
    harmony = android = None
    traces: dict[str, Trace] = {}
    if args.traces:
        traces = {t.trace_id: t for t in _load_traces(args.traces)}
    if args.harmony_device or args.bundle:
        if not (args.bundle and args.traces):
            raise SystemExit("错误：在线归因需要同时提供 --bundle 与 --traces")
        from .adapters.harmony import HarmonyAdapter
        _check_tool(cfg.device.hdc_path, "hdc")
        harmony = HarmonyAdapter(cfg, bundle=args.bundle, serial=args.harmony_device)
        harmony.ensure_ready()
        if args.pkg:
            from .adapters.android import AndroidAdapter
            _check_tool(cfg.device.adb_path, "adb")
            android = AndroidAdapter(cfg, pkg=args.pkg, serial=args.android_device)
            android.ensure_ready()

    page_pairs = PagePairs.load(args.page_pairs,
                                stem_sim_min=cfg.oracle.page_stem_sim_min)
    attribute_all(
        results, traces, cfg, page_pairs,
        harmony=harmony, android=android,
        confirm_runs=args.confirm_runs,
        tmp_root=os.path.join(args.out, "attribution_runs"),
    )

    metrics = compute_metrics(results)
    json_path, md_path = write_reports(metrics, results, args.out)
    logger.info("评估完成：\n  %s\n  %s", json_path, md_path)

    print("\n===== 汇总 =====")
    for key in ("traces", "R_replay", "R_eq_direct", "R_eq_policy",
                "direct_verified_steps", "mediated_steps",
                "external_recovery_failures", "trace_pass_rate",
                "avg_norm_divergence_depth", "page_coverage_align_rate",
                "widget_recall"):
        if key in metrics:
            print(f"  {key}: {metrics[key]}")
    return 0


# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="diff_tester",
        description="安卓→鸿蒙翻译差分模糊测试（record / replay / evaluate）",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="DEBUG 日志")
    sub = p.add_subparsers(dest="command", required=True)

    # record
    pr = sub.add_parser("record", help="安卓端探索 + 录制抽象事件轨迹")
    pr.add_argument("--pkg", required=True, help="安卓应用包名")
    pr.add_argument("--apk", help="可选：先安装该 APK")
    pr.add_argument("--device", help="adb 序列号（单设备可省略）")
    pr.add_argument("--traces", type=int, default=20, help="轨迹条数（默认 20）")
    pr.add_argument("--max-steps", type=int, default=30, help="每条最大步数（默认 30）")
    pr.add_argument("--seeds", help="种子轨迹目录（JSON，作为前缀）")
    pr.add_argument("--out", default="record_out", help="输出目录")
    pr.add_argument("--harmony-bundle", help="对应鸿蒙包名（写入轨迹 meta）")
    pr.add_argument("--config", help="config.yaml 覆盖默认配置")
    pr.set_defaults(func=cmd_record)

    # replay
    pp = sub.add_parser("replay", help="鸿蒙端语义回放 + 分叉检测")
    pp.add_argument("--bundle", required=True, help="鸿蒙应用包名（bundleName）")
    pp.add_argument("--hap", help="可选：先安装该 HAP")
    pp.add_argument("--device", help="hdc 序列号（单设备可省略）")
    pp.add_argument("--traces", required=True, help="轨迹 JSON 目录（record 的 traces/）")
    pp.add_argument("--page-pairs", help="页面对映射表 page_pairs.json")
    pp.add_argument("--out", default="results", help="结果输出目录")
    pp.add_argument("--config", help="config.yaml 覆盖默认配置")
    pp.set_defaults(func=cmd_replay)

    # evaluate
    pe = sub.add_parser("evaluate", help="归因 + 指标汇总 + 报告")
    pe.add_argument("--results", required=True, help="replay 的结果目录")
    pe.add_argument("--out", default="report", help="报告输出目录")
    pe.add_argument("--confirm-runs", type=int, default=3, help="复跑确认次数（默认 3）")
    pe.add_argument("--traces", help="轨迹目录（在线归因复跑用）")
    pe.add_argument("--bundle", help="鸿蒙包名（提供则做在线复跑确认）")
    pe.add_argument("--harmony-device", help="hdc 序列号")
    pe.add_argument("--pkg", help="安卓包名（提供则做 SOURCE_BUG 排除）")
    pe.add_argument("--android-device", help="adb 序列号")
    pe.add_argument("--page-pairs", help="页面对映射表 page_pairs.json")
    pe.add_argument("--whitelist", help="平台差异白名单 whitelist.yaml")
    pe.add_argument("--config", help="config.yaml 覆盖默认配置")
    pe.set_defaults(func=cmd_evaluate)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        logger.warning("用户中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())

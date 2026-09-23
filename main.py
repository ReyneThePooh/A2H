"""Android → HarmonyOS 翻译主入口（unit pipeline 链路）

流程：
1. OrderDeterminer   — 摘要生成、unit 划分、依赖分析、拓扑排序
2. ResourceMigrator  — Android res/ → HarmonyOS resources/，产出 .resource_mapping.json
3. TranslationPipeline — 按层翻译各 unit，产出 .ets 到生成工程
4. package_project   — 翻译产物套 DevEco 模板，注入权限、注册页面
5. BuildFixLoop      — assembleHap 构建 → 错误解析 → LLM 反思修复 → 重新构建验证
6. FunctionalFixLoop — （--enable-diff-gate）差分测试门禁：部署 HAP → 种子轨迹
   语义回放（L0–L2 预言）→ 分叉报告 → LLM 功能修复 → 重建 → 再门禁
"""

import argparse
import ctypes
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from hello_agents.core.path_config import (
    ANDROID_PROJECT_DIR,
    HARMONY_SOURCE_PROJECT_DIR,
    HARMONY_TEMPLATE_DIR,
    HARMONY_WORK_BASE_DIR,
)
from pipeline.order_determiner import OrderDeterminer
from pipeline.resource_migrator import (
    ResourceMigrator,
    parse_manifest,
)
from pipeline.unit_translator import TranslationPipeline, TranslationResult
from pipeline.project_packager import (
    package_project,
)
from pipeline.build_fixer import BuildFixLoop
from run_control import BudgetExceeded


if os.name == "nt":
    try:
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except (AttributeError, OSError):
        pass
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def find_manifest(android_root: Path) -> Path | None:
    for candidate in (
        android_root / "app" / "src" / "main" / "AndroidManifest.xml",
        android_root / "src" / "main" / "AndroidManifest.xml",
        android_root / "AndroidManifest.xml",
    ):
        if candidate.exists():
            return candidate
    return None


def print_translation_summary(results: list[TranslationResult]):
    ok = [r for r in results if r.success]
    failed = [r for r in results if not r.success]
    print(f"\n翻译结果: 成功 {len(ok)} / 失败 {len(failed)}")
    for r in failed:
        print(f"  ✗ {r.unit_name or r.file_name}: {r.error[:120]}")


def parse_optional_limit(value: str) -> int | None:
    """Parse a nonnegative limit; zero is the CLI spelling for unlimited."""
    limit = int(value)
    if limit < 0:
        raise argparse.ArgumentTypeError("资源上限不能为负数")
    return None if limit == 0 else limit


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析主流程参数；--resume 仅修复已生成的 HarmonyOS 工程。"""
    parser = argparse.ArgumentParser(
        description="Android → HarmonyOS 翻译与构建修复流程"
    )
    parser.add_argument(
        "--resume",
        metavar="PROJECT_DIR",
        help="跳过翻译和打包，继续修复指定的 HarmonyOS 工程",
    )
    parser.add_argument(
        "--sync-dir",
        metavar="SOURCE_DIR",
        help="使用 --resume 时，将修复同步到指定的生成工程目录",
    )
    parser.add_argument(
        "--max-fix-rounds",
        type=int,
        default=4,
        help="自动构建修复轮数上限（默认：4）",
    )
    parser.add_argument(
        "--enable-diff-gate",
        action="store_true",
        help="构建通过后运行差分测试门禁（轻量环内版）并进入功能修复环",
    )
    parser.add_argument(
        "--max-gate-rounds",
        type=int,
        default=3,
        help="完整差分回放轮数上限（默认：3，含初始回放）",
    )
    parser.add_argument(
        "--harmony-device",
        help="hdc 设备序列号（默认取 .env HARMONY_DEVICE；单设备可省略）",
    )
    parser.add_argument(
        "--seeds-dir",
        help="种子轨迹目录（默认取 .env DIFF_GATE_SEEDS_DIR 或门禁工作目录下 seeds/）",
    )
    parser.add_argument(
        "--gate-workspace",
        help="门禁工作目录（默认取 .env DIFF_GATE_WORKSPACE 或运行目录/gate）",
    )
    parser.add_argument(
        "--bundle",
        help="鸿蒙应用 bundleName（默认从工程 AppScope/app.json5 读取）",
    )
    parser.add_argument("--status", metavar="RUN_DIR", help="读取持久化阶段、预算与停止原因")
    parser.add_argument("--run-dir", help="本次运行的隔离目录（支持断点审计）")
    parser.add_argument("--run-id", help="运行标识，配合 --run-dir 恢复同一运行")
    parser.add_argument("--resume-run", help="从已保存运行目录恢复翻译/构建流程")
    parser.add_argument(
        "--max-model-calls", type=parse_optional_limit, default=None,
        help="模型调用上限（默认：无限；传 0 也表示无限）",
    )
    parser.add_argument("--max-builds", type=int, default=12)
    return parser.parse_args(argv)


def print_build_summary(fix_result: dict) -> None:
    """打印构建修复结果摘要。"""
    if fix_result["success"]:
        print(f"  构建: ✅ 通过（共 {fix_result['builds']} 次构建，"
              f"初始错误 {fix_result['initial_errors_count']} 个已全部解决）")
        return

    remaining = fix_result["remaining_errors"]
    print(f"  构建: ❌ 未通过（初始错误 {fix_result['initial_errors_count']} 个，"
          f"剩余 {fix_result['remaining_errors_count']} 个，需人工介入）")
    for i, err in enumerate(remaining[:5], 1):
        print(f"    {i}. {err['file']} L{err['line']}: {err['message'][:150]}")
    if len(remaining) > 5:
        print(f"    ... 还有 {len(remaining) - 5} 个")


def run_diff_gate_stage(project_dir: Path, sync_dir: Path | None, args) -> bool:
    """构建通过后的差分测试门禁 + 功能修复环（--enable-diff-gate）。

    契约文件（unit_page_map.json / page_pairs.json）从实际产物清单生成；
    Trace v2 种子轨迹需预先放在门禁工作目录 seeds/ 下
    （或用 --seeds-dir 指定）。
    """
    from pipeline.functional_fixer import FunctionalFixLoop

    workspace = Path(
        args.gate_workspace
        or os.getenv("DIFF_GATE_WORKSPACE", "").strip()
        or (project_dir / ".diff_gate")
    )
    seeds_dir = args.seeds_dir or os.getenv("DIFF_GATE_SEEDS_DIR", "").strip() or None
    device = args.harmony_device or os.getenv("HARMONY_DEVICE", "").strip() or None
    hdc_path = os.getenv("HDC_PATH", "").strip() or None

    plan_path = project_dir / ".pipeline_cache" / "translation_plan.json"
    if not plan_path.exists():
        # Older projects may retain the planner cache beside Android sources.
        fallback = Path(ANDROID_PROJECT_DIR) / ".pipeline_cache" / "translation_plan.json"
        plan_path = fallback if fallback.exists() else None

    print(f"\n{'=' * 60}")
    print("🚦 差分测试门禁（轻量环内版）")
    print(f"{'=' * 60}")
    print(f"  工作目录: {workspace}")

    loop = FunctionalFixLoop(max_gate_rounds=args.max_gate_rounds,
                             build_fix_rounds=args.max_fix_rounds)
    try:
        result = loop.run(
            project_dir, sync_dir, workspace,
            seeds_dir=seeds_dir, bundle=args.bundle,
            device=device, hdc_path=hdc_path, plan_path=plan_path,
        )
    except BudgetExceeded:
        raise
    except (FileNotFoundError, RuntimeError) as e:
        print(f"\n❌ 门禁无法运行: {e}")
        return False

    if result["success"]:
        print(f"\n✅ 功能一致性门禁通过（共 {result['rounds']} 轮）")
    else:
        n = len(result.get("needs_human", []))
        print(f"\n❌ 门禁未通过，{n} 个缺陷挂起待人工介入"
              f"（分叉报告与历史见 {workspace / 'history'}）")
    return bool(result["success"])


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = parse_args(argv)
    from pipeline.runner import execute
    return execute(args, cli_args=argv if argv is not None else sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())

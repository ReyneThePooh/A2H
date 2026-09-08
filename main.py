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
import time
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
    reset_generated_artifacts,
)
from pipeline.unit_translator import TranslationPipeline, TranslationResult
from pipeline.project_packager import (
    ResourceValidationError,
    package_project,
    rewrite_flat_imports,
)
from pipeline.build_fixer import BuildFixLoop


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
        default=3,
        help="自动构建修复轮数上限（默认：3）",
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
        help="门禁-修复迭代轮数上限（默认：3）",
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
        help="门禁工作目录（默认取 .env DIFF_GATE_WORKSPACE 或 <工程>/.diff_gate）",
    )
    parser.add_argument(
        "--bundle",
        help="鸿蒙应用 bundleName（默认从工程 AppScope/app.json5 读取）",
    )
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

    契约文件（unit_page_map.json / page_pairs.json）自动从
    translation_plan.json 生成；种子轨迹需预先放在门禁工作目录 seeds/ 下
    （或用 --seeds-dir 指定）。
    """
    from pipeline.functional_fixer import FunctionalFixLoop
    from pipeline.gate_bridge import prepare_workspace

    workspace = Path(
        args.gate_workspace
        or os.getenv("DIFF_GATE_WORKSPACE", "").strip()
        or (project_dir / ".diff_gate")
    )
    seeds_dir = args.seeds_dir or os.getenv("DIFF_GATE_SEEDS_DIR", "").strip() or None
    device = args.harmony_device or os.getenv("HARMONY_DEVICE", "").strip() or None
    hdc_path = os.getenv("HDC_PATH", "").strip() or None

    plan_path = Path(ANDROID_PROJECT_DIR) / ".pipeline_cache" / "translation_plan.json"
    if plan_path.exists():
        prepare_workspace(workspace, plan_path)
    else:
        print(f"⚠️ 找不到 {plan_path}，无法生成契约文件；增量选择将退化为全量回放")
        plan_path = None

    print(f"\n{'=' * 60}")
    print("🚦 差分测试门禁（轻量环内版）")
    print(f"{'=' * 60}")
    print(f"  工作目录: {workspace}")

    loop = FunctionalFixLoop(max_gate_rounds=args.max_gate_rounds)
    try:
        result = loop.run(
            project_dir, sync_dir, workspace,
            seeds_dir=seeds_dir, bundle=args.bundle,
            device=device, hdc_path=hdc_path, plan_path=plan_path,
        )
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

    if args.max_fix_rounds < 0:
        print("❌ --max-fix-rounds 必须大于或等于 0")
        return 2

    if args.max_gate_rounds < 1:
        print("❌ --max-gate-rounds 必须大于或等于 1")
        return 2

    if args.sync_dir and not args.resume:
        print("❌ --sync-dir 只能与 --resume 一起使用")
        return 2

    if args.resume:
        project_dir = Path(args.resume)
        if not project_dir.is_dir():
            print(f"❌ 指定工程目录不存在: {project_dir}")
            return 2
        sync_dir = Path(args.sync_dir) if args.sync_dir else None
        if sync_dir and not sync_dir.is_dir():
            print(f"❌ 指定同步目录不存在: {sync_dir}")
            return 2
        print(f"继续修复已有工程: {project_dir}")
        if sync_dir:
            print(f"修复结果将同步到: {sync_dir}")
        rewrite_flat_imports(project_dir / "entry" / "src" / "main" / "ets" / "pages")
        fix_result = BuildFixLoop(max_fix_rounds=args.max_fix_rounds).run(
            project_dir, sync_dir=sync_dir
        )
        print_build_summary(fix_result)
        if not fix_result["success"]:
            return 1
        if args.enable_diff_gate:
            return 0 if run_diff_gate_stage(project_dir, sync_dir, args) else 1
        return 0

    android_root = Path(ANDROID_PROJECT_DIR)
    generated_root = Path(HARMONY_SOURCE_PROJECT_DIR)
    template_root = Path(HARMONY_TEMPLATE_DIR)
    packaged_root = HARMONY_WORK_BASE_DIR.parent / (
        f"{HARMONY_WORK_BASE_DIR.name}_{time.strftime('%Y%m%d_%H%M%S')}"
    )

    # 1. 翻译顺序确定（摘要 → unit 划分 → 依赖 → 拓扑排序）
    determiner = OrderDeterminer(str(android_root))
    layers, deps = determiner.run()

    # 2. 静态资源迁移（翻译前执行，翻译器依赖 .resource_mapping.json 提供资源提示）
    reset_generated_artifacts(generated_root)
    migrator = ResourceMigrator(str(android_root), str(generated_root))
    migrator.run()

    # 3. 按层翻译
    translation = TranslationPipeline(
        project_root=str(android_root),
        harmony_root=str(generated_root),
        summaries=determiner.summaries,
        resource_mapping_path=str(generated_root / ".resource_mapping.json"),
    )
    results = translation.run(layers, deps)
    print_translation_summary(results)

    if not any(r.success for r in results):
        print("\n❌ 没有任何 unit 翻译成功，跳过打包与构建")
        return 1

    # 4. 套模板打包
    manifest = find_manifest(android_root)
    permissions = parse_manifest(str(manifest))["permissions"] if manifest else []
    try:
        package_project(
            template_dir=template_root,
            generated_dir=generated_root,
            output_dir=packaged_root,
            android_permissions=permissions,
        )
    except ResourceValidationError as error:
        print(f"\n❌ {error}")
        return 1

    # 5. 构建 + 反思修复闭环（修复写入打包工程，并同步回生成工程）
    fix_result = BuildFixLoop(max_fix_rounds=args.max_fix_rounds).run(
        packaged_root, sync_dir=generated_root
    )

    print(f"\n{'=' * 60}")
    print("🎯 最终结果")
    print(f"{'=' * 60}")
    print(f"  翻译: 成功 {sum(r.success for r in results)} / 共 {len(results)} 个输出")
    print(f"  工程: {packaged_root}")
    print_build_summary(fix_result)
    if not fix_result["success"]:
        return 1

    # 6. 差分测试门禁 + 功能修复环（可选，--enable-diff-gate）
    if args.enable_diff_gate:
        return 0 if run_diff_gate_stage(packaged_root, generated_root, args) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

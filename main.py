"""Android → HarmonyOS 翻译主入口（unit pipeline 链路）

流程：
1. OrderDeterminer   — 摘要生成、unit 划分、依赖分析、拓扑排序
2. ResourceMigrator  — Android res/ → HarmonyOS resources/，产出 .resource_mapping.json
3. TranslationPipeline — 按层翻译各 unit，产出 .ets 到生成工程
4. package_project   — 翻译产物套 DevEco 模板，注入权限、注册页面
5. BuildFixLoop      — assembleHap 构建 → 错误解析 → LLM 反思修复 → 重新构建验证
"""

import argparse
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
from pipeline.resource_migrator import ResourceMigrator, parse_manifest
from pipeline.unit_translator import TranslationPipeline, TranslationResult
from pipeline.project_packager import package_project, rewrite_flat_imports
from pipeline.build_fixer import BuildFixLoop

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


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = parse_args(argv)

    if args.max_fix_rounds < 0:
        print("❌ --max-fix-rounds 必须大于或等于 0")
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
        return 0 if fix_result["success"] else 1

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
    package_project(
        template_dir=template_root,
        generated_dir=generated_root,
        output_dir=packaged_root,
        android_permissions=permissions,
    )

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
    return 0 if fix_result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())

"""Sequential orchestration with checkpoints for the current working project."""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from run_control import (Budget, BudgetExceeded, FileLock, LockUnavailable,
                         ResumeMismatch, RunJournal,
                         budget_scope, file_sha256, fingerprint)
from pipeline.artifacts import (
    repair_generated_contract, project_source_fingerprint,
    validate_project_contract,
)


class StageFailure(RuntimeError):
    def __init__(self, reason, details=None):
        self.reason, self.details = reason, details
        super().__init__(reason)


def tree_digest(root):
    root = Path(root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(root)
    entries = {}
    excluded = {".git", ".gradle", ".pipeline_cache", "build", ".hvigor", "oh_modules",
                "node_modules", "__pycache__", ".idea", ".diff_gate"}
    for directory, children, files in os.walk(root):
        children[:] = sorted(d for d in children if d.lower() not in excluded)
        for name in children:
            child = Path(directory) / name
            if child.is_symlink() or (hasattr(child, "is_junction") and child.is_junction()):
                raise ValueError(f"Input directory links require explicit materialization: {child}")
        for filename in sorted(files):
            path = Path(directory) / filename
            if filename.startswith(".env") or filename.endswith((".log", ".lock")):
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError(f"Input file escapes source tree: {path}")
            entries[path.relative_to(root).as_posix()] = file_sha256(path)
    return fingerprint(entries)


def require_contract(root):
    issues = validate_project_contract(root)
    if issues:
        raise StageFailure("TRANSLATION_CONTRACT_INVALID", issues)


def build_is_current(root):
    """Resume only a real, unchanged build with existing verified artifacts."""
    try:
        if (root / ".pipeline_build_validation_pending").exists():
            return False
        data = json.loads((root / ".pipeline_build.json").read_text(encoding="utf-8"))
        current = (
            data.get("status") == "success"
            and bool(data.get("artifacts"))
            and data["project_input_sha256"] == project_source_fingerprint(root)
            and all(
                (root / artifact["path"]).resolve().is_relative_to(root.resolve())
                and file_sha256(root / artifact["path"]) == artifact["sha256"]
                for artifact in data["artifacts"]
            )
        )
        if not current:
            return False
        from pipeline.project_packager import find_component_new_violations
        return not find_component_new_violations(root)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def restore_options(args, cli_args, saved):
    supplied = {arg.split("=", 1)[0] for arg in cli_args if arg.startswith("--")}
    for key, value in saved.items():
        if key in {"resume_run", "run_dir", "run_id", "status"} or not hasattr(args, key):
            continue
        if "--" + key.replace("_", "-") not in supplied:
            setattr(args, key, value)


def show_status(directory):
    root = Path(directory).resolve()
    state = json.loads((root / "state.json").read_text(encoding="utf-8"))
    # A process merely existing is never evidence of useful progress.
    try:
        with FileLock(root / ".run.lock"):
            owner = "idle"
    except LockUnavailable:
        owner = "active"
    summary = {key: state.get(key) for key in (
        "run_id", "status", "current_stage", "updated_at", "stop_reason",
        "generated_root", "packaged_root", "budget", "stages")}
    summary["lock_owner"] = owner
    if owner == "idle" and state.get("status") == "running":
        summary["status"] = "INTERRUPTED"
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def execute(args, cli_args=()):
    # Keep main's configurable constants and small helper functions patchable.
    import main as entry
    journal = budget = None
    try:
        if args.status:
            return show_status(args.status)
        if args.resume_run and args.run_dir and Path(args.resume_run).resolve() != Path(args.run_dir).resolve():
            raise ValueError("--resume-run 与 --run-dir 指向不同目录")
        if args.resume_run:
            run_root = Path(args.resume_run).resolve()
            saved = json.loads((run_root / "state.json").read_text(encoding="utf-8"))
            restore_options(args, cli_args, saved.get("options", {}))
        else:
            base = Path(entry.HARMONY_WORK_BASE_DIR).resolve().parent / ".a2h_runs"
            run_root = Path(args.run_dir).resolve() if args.run_dir else base / (
                time.strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8])
            if (run_root / "state.json").exists():
                raise ValueError("运行目录已存在；使用 --resume-run 恢复或选择新目录")
            if run_root.exists() and any(run_root.iterdir()):
                raise ValueError("新运行需要空目录，现有文件已保留")
        if args.max_fix_rounds < 0 or args.max_gate_rounds < 1:
            raise ValueError("构建修复轮数须 >= 0，差分回放轮数须 >= 1")
        if args.sync_dir and not args.resume:
            raise ValueError("--sync-dir 只能用于 --resume PROJECT_DIR")
        if any(v is not None and v < 0 for v in (args.max_model_calls, args.max_builds)):
            raise ValueError("资源上限不能为负数")
        run_root.mkdir(parents=True, exist_ok=True)
        with FileLock(run_root / ".run.lock"):
            journal = RunJournal(run_root, run_id=args.run_id)
            # The pipeline has no global wall-clock deadline. Individual model,
            # build and gate operations retain their own bounded timeouts.
            budget = Budget(None, args.max_model_calls, args.max_builds)
            if args.resume_run and journal.state.get("budget"):
                budget.restore(journal.state["budget"])
            budget.snapshot_callback = lambda snapshot: journal.update(budget=snapshot)
            with budget_scope(budget):
                return run_stages(entry, args, run_root, journal, budget)
    except BudgetExceeded as exc:
        if journal:
            journal.update(status="BUDGET_EXHAUSTED", stop_reason=exc.reason)
        print(f"预算已耗尽：{exc.reason}；已保留检查点和当前工程。")
        return 1
    except KeyboardInterrupt:
        if journal:
            journal.update(status="INTERRUPTED", stop_reason="USER_INTERRUPT")
        print("运行已中断，当前工程与检查点已保留。")
        return 130
    except Exception as exc:
        reason = getattr(exc, "reason", type(exc).__name__)
        if journal:
            journal.update(status="FAILED", stop_reason=reason,
                           details=getattr(exc, "details", None))
        print(f"流程停止：{reason} — {exc}")
        if getattr(exc, "details", None):
            print(json.dumps(exc.details, ensure_ascii=False, indent=2))
        return 1
    finally:
        if journal and budget:
            journal.update(budget=budget.snapshot())


def run_stages(entry, args, run_root, journal, budget):
    from run_control import atomic_json
    android = Path(entry.ANDROID_PROJECT_DIR).resolve()
    template = Path(entry.HARMONY_TEMPLATE_DIR).resolve()
    generated = Path(args.sync_dir).resolve() if args.sync_dir else run_root / "generated"
    packaged = Path(args.resume).resolve() if args.resume else run_root / "packaged"
    if args.resume and not packaged.is_dir():
        raise FileNotFoundError(packaged)
    if args.resume and (packaged / "translation_manifest.json").is_file():
        from pipeline.artifacts import load_artifact_manifest
        android = Path(load_artifact_manifest(packaged)["source_root"]).resolve()
    if run_root.is_relative_to(android) or run_root.is_relative_to(template):
        raise ValueError("运行目录不能位于 Android 源码或 Harmony 模板内部")
    if args.resume and (run_root.is_relative_to(packaged) or
                        args.sync_dir and run_root.is_relative_to(generated)):
        raise ValueError("运行目录不能位于待修复工程或同步目录内部")
    seeds = args.seeds_dir or os.getenv("DIFF_GATE_SEEDS_DIR", "").strip()
    if not args.gate_workspace:
        args.gate_workspace = os.getenv("DIFF_GATE_WORKSPACE", "").strip() or str(run_root / "gate")
    if args.enable_diff_gate and not seeds:
        seeds = str(Path(args.gate_workspace) / "seeds")
    if seeds:
        args.seeds_dir = str(Path(seeds).resolve())
    args.gate_workspace = str(Path(args.gate_workspace).resolve())
    if not args.harmony_device:
        args.harmony_device = os.getenv("HARMONY_DEVICE", "").strip() or None
    engine_root = Path(__file__).resolve().parent.parent
    engine_files = [engine_root / "main.py", engine_root / "run_control.py"]
    engine_files += list((engine_root / "pipeline").glob("*.py"))
    engine_files += list((engine_root / "diff_tester").rglob("*.py"))
    inputs = {
        "android_root": str(android), "android_sha256": tree_digest(android),
        "packaged_root": str(packaged), "sync_root": str(generated) if args.sync_dir else None,
        "gate_workspace": args.gate_workspace,
        "template_root": str(template),
        "template_sha256": tree_digest(template) if not args.resume else None,
        "engine_sha256": fingerprint({p.relative_to(engine_root).as_posix(): file_sha256(p) for p in engine_files}),
        "seeds_root": args.seeds_dir,
        "seeds_sha256": tree_digest(args.seeds_dir) if args.enable_diff_gate and args.seeds_dir else None,
        "gate_enabled": args.enable_diff_gate, "device": args.harmony_device, "bundle": args.bundle,
        "config_sha256": fingerprint({
            "config_yaml": file_sha256(engine_root / "config.yaml"),
            "environment": {k: os.getenv(k, "") for k in (
                "LLM_MODEL_ID", "LLM_BASE_URL", "PIPELINE_MODEL", "LLM_PROVIDER",
                "NODE_HOME", "HDC_PATH", "LLM_TIMEOUT", "LLM_MAX_ATTEMPTS")}}),
    }
    from pipeline.unit_translator import UnitTranslator
    inputs["sdk_sha256"] = fingerprint(UnitTranslator._sdk_fingerprint_inputs())
    journal.bind_inputs(inputs, resume=bool(args.resume_run))
    journal.update(options=vars(args), generated_root=str(generated), packaged_root=str(packaged),
                   status="running", stop_reason=None)

    def stage_done(name):
        return journal.state.get("stages", {}).get(name, {}).get("status") == "completed"

    def checkpoint(name, root):
        # Hash the material stage outputs; resumed runs never silently trust a
        # manually edited or half-written tree merely because a JSON flag says done.
        digest = project_source_fingerprint(root)
        journal.update(**{name + "_sha256": digest})
        journal.update(budget=budget.snapshot())

    if args.resume and args.resume_run:
        previous = journal.state.get("packaged_sha256")
        if previous and previous != project_source_fingerprint(packaged):
            raise ResumeMismatch("Repair project changed since the last checkpoint")

    if not args.resume:
        with FileLock(android / ".pipeline.lock"):
            if stage_done("translation"):
                require_contract(generated)
                if journal.state.get("generated_sha256") != project_source_fingerprint(generated):
                    raise ResumeMismatch("Generated output changed since checkpoint")
            else:
                from pipeline.planning_checkpoint import save_planning, load_planning
                planning_path = run_root / "planning.json"
                if stage_done("planning"):
                    layers, deps, summaries = load_planning(planning_path)
                else:
                    with journal.stage("planning"):
                        determiner = entry.OrderDeterminer(str(android))
                        layers, deps = determiner.run()
                        summaries = determiner.summaries
                        save_planning(planning_path, layers, deps, summaries)
                with journal.stage("translation"):
                    # This is a run-local tree. The user's accepted HarmonyProject
                    # is never deleted. Translation's own checkpoints resume steps.
                    entry.ResourceMigrator(str(android), str(generated)).run()
                    translation = entry.TranslationPipeline(
                        project_root=str(android), harmony_root=str(generated),
                        summaries=summaries,
                        resource_mapping_path=str(generated / ".resource_mapping.json"))
                    results = translation.run(layers, deps)
                    entry.print_translation_summary(results)
                    if not results or any(not result.success for result in results):
                        raise StageFailure("TRANSLATION_INCOMPLETE", [
                            {"unit": result.unit_name, "file": result.file_name,
                             "status": result.status, "error": result.error}
                            for result in results if not result.success])
                    issues = repair_generated_contract(generated)
                    if issues:
                        raise StageFailure("TRANSLATION_CONTRACT_INVALID", issues)
                    checkpoint("generated", generated)
        if stage_done("package"):
            if (not build_is_current(packaged) and journal.state.get("packaged_sha256")
                    != project_source_fingerprint(packaged)):
                raise ResumeMismatch("Packaged source changed since the last checkpoint")
        else:
            with journal.stage("package"):
                manifest = entry.find_manifest(android)
                permissions = entry.parse_manifest(str(manifest))["permissions"] if manifest else []
                # An interrupted partial package is retained, not removed.
                if packaged.exists():
                    retained = run_root / ("packaged_interrupted_" + uuid.uuid4().hex[:8])
                    packaged.rename(retained)
                    journal.event("partial_package_retained", path=str(retained))
                entry.package_project(template, generated, packaged, android_permissions=permissions)
                require_contract(packaged)
                checkpoint("packaged", packaged)

    # Reuse only a real, unchanged build. Failed repairs stay in this project
    # and are checkpointed so the next run continues from the latest source.
    if not (stage_done("build") and build_is_current(packaged)):
        with journal.stage("build"):
            try:
                result = entry.BuildFixLoop(max_fix_rounds=args.max_fix_rounds).run(
                    packaged, sync_dir=generated if args.sync_dir else None,
                    workspace=run_root / "build")
            finally:
                checkpoint("packaged", packaged)
                if generated.is_dir():
                    checkpoint("generated", generated)
            atomic_json(run_root / "build_result.json", result)
            entry.print_build_summary(result)
            if not result["success"]:
                raise StageFailure(result.get("stop_reason", "BUILD_FAILED"), result)
            if not build_is_current(packaged):
                raise StageFailure("BUILD_PROVENANCE_INVALID")

    if args.enable_diff_gate:
        with journal.stage("functional_repair"):
            try:
                passed = entry.run_diff_gate_stage(packaged,
                    generated if args.sync_dir else None, args)
            finally:
                checkpoint("packaged", packaged)
                if generated.is_dir():
                    checkpoint("generated", generated)
            if not passed:
                raise StageFailure("FUNCTIONAL_GATE_NOT_PASSED")
    journal.update(status="COMPLETED", stop_reason="FULL_GATE_PASSED" if args.enable_diff_gate else "BUILD_VERIFIED")
    print(f"工程：{packaged}\n运行记录：{run_root}\n"
          + ("差分测试全量通过。" if args.enable_diff_gate else "构建已验证；尚未执行行为一致性验证。"))
    return 0

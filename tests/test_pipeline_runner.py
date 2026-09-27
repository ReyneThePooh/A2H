"""Offline orchestration regressions: partial work, resume and provenance."""
import json
import shutil
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

import main
from pipeline import runner
from pipeline.artifacts import (build_translation_manifest, write_artifact_manifest,
                                project_source_fingerprint)
from pipeline.order_determiner import Unit
from analyzers.static import FileSummary
from pipeline.unit_translator import TranslationResult
from run_control import atomic_json, consume_budget, file_sha256


@pytest.fixture
def setup_pipeline(tmp_path, monkeypatch):
    android, template, accepted = (tmp_path / x for x in ("android", "template", "accepted"))
    for root in (android, template, accepted):
        root.mkdir()
    (android / "Main.java").write_text("class Main {}", encoding="utf-8")
    (accepted / "keep.ets").write_text("accepted source", encoding="utf-8")
    monkeypatch.setattr(main, "ANDROID_PROJECT_DIR", android)
    monkeypatch.setattr(main, "HARMONY_TEMPLATE_DIR", template)
    monkeypatch.setattr(main, "HARMONY_SOURCE_PROJECT_DIR", accepted)
    monkeypatch.setattr(main, "HARMONY_WORK_BASE_DIR", tmp_path / "Work")
    monkeypatch.setattr(main, "load_dotenv", lambda: None)
    counts = {"translation": 0, "package": 0, "build": 0}
    unit = Unit("main", ["Main.java"])
    summaries = {"Main.java": FileSummary(file_path="Main.java", package="example", class_name="Main")}
    class Determiner:
        def __init__(self, *args): self.summaries = summaries
        def run(self): return [[unit]], {}
    class Migrator:
        def __init__(self, *args): pass
        def run(self): pass
    class Translator:
        def __init__(self, **kwargs): self.root = Path(kwargs["harmony_root"])
        def run(self, *args):
            counts["translation"] += 1
            consume_budget("llm_calls")
            code = "@Entry\n@Component\nstruct Main { build() { Text('hello') } }"
            result = TranslationResult(unit_name="main", file_name="Actual.ets", code=code,
                                       success=True, sources=["Main.java"])
            target = self.root / "entry/src/main/ets/pages/Actual.ets"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(code, encoding="utf-8", newline="")
            manifest = build_translation_manifest(android, [unit], [result], summaries,
                {"activities": [{"name": "example.Main"}],
                 "launchers": [{"activity": "example.Main", "component": "example.Main"}]})
            write_artifact_manifest(self.root, manifest)
            return [result]
    def package(template, generated, output, **kwargs):
        counts["package"] += 1
        shutil.copytree(generated, output)
    class Builder:
        def __init__(self, *args, **kwargs): pass
        def run(self, root, **kwargs):
            counts["build"] += 1
            consume_budget("builds")
            hap = root / "entry/build/default/outputs/entry-signed.hap"
            hap.parent.mkdir(parents=True, exist_ok=True)
            hap.write_bytes(b"verified fake HAP")
            atomic_json(root / ".pipeline_build.json", {
                "status": "success", "project_input_sha256": project_source_fingerprint(root),
                "artifacts": [{"path": hap.relative_to(root).as_posix(),
                               "sha256": file_sha256(hap), "signed": True}]})
            return {"success": True, "builds": 1, "initial_errors_count": 0}
    monkeypatch.setattr(main, "OrderDeterminer", Determiner)
    monkeypatch.setattr(main, "ResourceMigrator", Migrator)
    monkeypatch.setattr(main, "TranslationPipeline", Translator)
    monkeypatch.setattr(main, "package_project", package)
    monkeypatch.setattr(main, "BuildFixLoop", Builder)
    return NS(android=android, accepted=accepted, counts=counts, run=tmp_path / "run",
              Translator=Translator, Builder=Builder)


def test_partial_translation_never_packages_or_deletes_accepted(setup_pipeline, monkeypatch):
    ctx = setup_pipeline
    monkeypatch.setattr(ctx.Translator, "run", lambda *a: [
        TranslationResult(unit_name="ok", success=True),
        TranslationResult(unit_name="failed", success=False, error="step 2 failed")])
    assert main.main(["--run-dir", str(ctx.run)]) == 1
    assert ctx.counts["package"] == 0 and ctx.counts["build"] == 0
    assert (ctx.accepted / "keep.ets").read_text() == "accepted source"
    state = json.loads((ctx.run / "state.json").read_text())
    assert state["stop_reason"] == "TRANSLATION_INCOMPLETE"
    assert state["stages"]["translation"]["status"] == "failed"
    assert state["details"][0]["unit"] == "failed"
    assert state["details"][0]["error"] == "step 2 failed"


def test_resume_reuses_verified_stages_and_keeps_budget(setup_pipeline):
    ctx = setup_pipeline
    assert main.main(["--run-dir", str(ctx.run)]) == 0
    assert main.main(["--resume-run", str(ctx.run)]) == 0
    assert ctx.counts == {"translation": 1, "package": 1, "build": 1}
    state = json.loads((ctx.run / "state.json").read_text())
    assert state["budget"]["counts"]["llm_calls"] == 1
    assert state["budget"]["time_limit_s"] is None
    assert state["stop_reason"] == "BUILD_VERIFIED"


def test_resume_rejects_source_change_even_with_same_timestamp(setup_pipeline):
    import os
    ctx = setup_pipeline
    assert main.main(["--run-dir", str(ctx.run)]) == 0
    source = ctx.android / "Main.java"
    old = source.stat()
    source.write_text("class Different {}")
    os.utime(source, ns=(old.st_atime_ns, old.st_mtime_ns))
    assert main.main(["--resume-run", str(ctx.run)]) == 1
    assert ctx.counts["translation"] == 1
    assert json.loads((ctx.run / "state.json").read_text())["stop_reason"] == "ResumeMismatch"


def test_budget_exhaustion_is_durable_and_can_resume_with_larger_limit(setup_pipeline):
    ctx = setup_pipeline
    assert main.main(["--run-dir", str(ctx.run), "--max-builds", "0"]) == 1
    state = json.loads((ctx.run / "state.json").read_text())
    assert state["status"] == "BUDGET_EXHAUSTED"
    assert state["budget"]["counts"]["llm_calls"] == 1
    assert main.main(["--resume-run", str(ctx.run), "--max-builds", "1"]) == 0
    assert ctx.counts["translation"] == 1


def test_failed_build_retains_current_repair_for_next_run(tmp_path, monkeypatch):
    import subprocess
    from pipeline.build_fixer import BuildFixLoop
    import pipeline.build_fixer as build
    root = tmp_path / "project"
    source = root / "entry/src/main/ets/pages/Main.ets"
    source.parent.mkdir(parents=True)
    source.write_text("class Main { method() { return 1; } }")
    latest = "class Main { method() { return 2; } }"
    monkeypatch.setattr(build, "run_hvigor", lambda *a: subprocess.CompletedProcess([], 1, "", "failed"))
    monkeypatch.setattr(build.ErrorParser, "parse", lambda *a: [
        {"file": str(source), "line": 1, "message": "type error", "code": "TYPE_ERROR"}])
    monkeypatch.setattr(BuildFixLoop, "_fix_files", lambda *a: source.write_text(latest))
    result = BuildFixLoop(llm=object(), max_fix_rounds=1).run(root, workspace=tmp_path / "work")
    assert not result["success"]
    assert source.read_text() == latest
    assert result["builds"] == 2
    assert not (tmp_path / "work/transactions").exists()
    assert json.loads((tmp_path / "work/build_result.json").read_text())["status"] == "BUILD_FAILED"
    assert BuildFixLoop._locate(root, "../../escape.ets") is None

    resumed = BuildFixLoop(llm=object(), max_fix_rounds=1).run(
        root, workspace=tmp_path / "work"
    )
    assert resumed["stop_reason"] == "NO_PROGRESS_PERSISTED"
    assert resumed["builds"] == 0


def test_post_build_validation_failure_invalidates_build_reuse(tmp_path, monkeypatch):
    import subprocess
    import pipeline.build_fixer as build
    root = tmp_path / "project"
    source = root / "entry/src/main/ets/pages/Main.ets"
    source.parent.mkdir(parents=True)
    source.write_text("@Component\nstruct Child { build() {} }\nconst value = new Child();")
    hap = root / "entry/build/default/outputs/test.hap"
    hap.parent.mkdir(parents=True)
    hap.write_bytes(b"offline artifact")
    def compile_success(root):
        atomic_json(root / ".pipeline_build.json", {
            "status": "success", "project_input_sha256": project_source_fingerprint(root),
            "artifacts": [{"path": hap.relative_to(root).as_posix(), "sha256": file_sha256(hap)}]})
        return subprocess.CompletedProcess([], 0, "", "")
    monkeypatch.setattr(build, "run_hvigor", compile_success)
    result = build.BuildFixLoop(llm=object(), max_fix_rounds=0).run(root)
    assert not result["success"]
    assert result["remaining_errors"][0]["code"] == "ARKUI_NEW_COMPONENT"
    assert not runner.build_is_current(root)
    proof = json.loads((root / ".pipeline_build.json").read_text())
    assert proof["status"] == "failed"


def test_pending_post_build_validation_prevents_build_reuse(tmp_path):
    root = tmp_path / "project"
    artifact = root / "entry/build/default/outputs/app.hap"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"compiled")
    atomic_json(root / ".pipeline_build.json", {
        "status": "success",
        "project_input_sha256": project_source_fingerprint(root),
        "artifacts": [{
            "path": artifact.relative_to(root).as_posix(),
            "sha256": file_sha256(artifact),
        }],
    })
    (root / ".pipeline_build_validation_pending").write_text(
        "pending", encoding="utf-8"
    )

    assert not runner.build_is_current(root)


@pytest.mark.parametrize("interrupted", [False, True])
def test_resume_continues_in_place_build_repair(setup_pipeline, monkeypatch, interrupted):
    ctx = setup_pipeline
    original_run = ctx.Builder.run
    def unfinished(self, root, **kwargs):
        source = root / "entry/src/main/ets/pages/Actual.ets"
        source.write_text(source.read_text() + "\n// partially repaired")
        if interrupted:
            raise KeyboardInterrupt()
        return {"success": False, "builds": 1, "initial_errors_count": 1,
                "remaining_errors_count": 1, "remaining_errors": []}
    monkeypatch.setattr(ctx.Builder, "run", unfinished)
    assert main.main(["--run-dir", str(ctx.run)]) == (130 if interrupted else 1)
    source = ctx.run / "packaged/entry/src/main/ets/pages/Actual.ets"
    assert "partially repaired" in source.read_text()
    state = json.loads((ctx.run / "state.json").read_text())
    assert state["packaged_sha256"] == project_source_fingerprint(ctx.run / "packaged")
    monkeypatch.setattr(ctx.Builder, "run", original_run)
    assert main.main(["--resume-run", str(ctx.run)]) == 0
    assert ctx.counts["translation"] == 1
    assert "partially repaired" in source.read_text()


def test_resume_build_accepts_legacy_project_without_manifest(setup_pipeline):
    ctx = setup_pipeline
    assert main.main(["--resume", str(ctx.accepted), "--run-dir", str(ctx.run)]) == 0
    assert ctx.counts == {"translation": 0, "package": 0, "build": 1}


def test_resume_existing_project_detects_edits_after_checkpoint(setup_pipeline):
    ctx = setup_pipeline
    assert main.main(["--resume", str(ctx.accepted), "--run-dir", str(ctx.run)]) == 0
    (ctx.accepted / "keep.ets").write_text("external edit")
    assert main.main(["--resume-run", str(ctx.run)]) == 1
    assert ctx.counts["build"] == 1
    state = json.loads((ctx.run / "state.json").read_text())
    assert state["stop_reason"] == "ResumeMismatch"


def test_gate_bridge_refuses_stale_build_before_deploy(tmp_path, monkeypatch):
    from pipeline.gate_bridge import run_diff_gate
    import pipeline.gate_bridge as bridge
    source = tmp_path / "entry/src/main/ets/pages/Main.ets"
    source.parent.mkdir(parents=True)
    source.write_text("class Main {}")
    atomic_json(tmp_path / ".pipeline_build.json", {"status": "success",
                "project_input_sha256": "old", "artifacts": []})
    monkeypatch.setattr(bridge, "run_gate", lambda *args: pytest.fail("must not deploy"))
    with pytest.raises(RuntimeError, match="STALE_BUILD"):
        run_diff_gate(tmp_path, tmp_path / "gate", [], bundle="example")


def test_translation_interruption_reuses_frozen_plan(setup_pipeline, monkeypatch):
    ctx = setup_pipeline
    original_run = ctx.Translator.run
    def interrupted(*args):
        raise KeyboardInterrupt()
    monkeypatch.setattr(ctx.Translator, "run", interrupted)
    assert main.main(["--run-dir", str(ctx.run)]) == 130
    monkeypatch.setattr(ctx.Translator, "run", original_run)
    monkeypatch.setattr(main, "OrderDeterminer", lambda *a: pytest.fail("must reuse saved plan"))
    assert main.main(["--resume-run", str(ctx.run)]) == 0
    assert ctx.counts["translation"] == 1


def test_resume_cannot_redirect_to_different_project(setup_pipeline):
    ctx = setup_pipeline
    assert main.main(["--run-dir", str(ctx.run)]) == 0
    other = ctx.run.parent / "other"
    shutil.copytree(ctx.run / "packaged", other)
    assert main.main(["--resume-run", str(ctx.run), "--resume", str(other)]) == 1
    assert ctx.counts["build"] == 1


def test_unknown_build_metadata_is_never_reused(tmp_path):
    (tmp_path / ".pipeline_build.json").write_text("[]")
    assert not runner.build_is_current(tmp_path)


def test_status_marks_abandoned_running_journal_interrupted(tmp_path, capsys):
    atomic_json(tmp_path / "state.json", {"status": "running", "current_stage": "translation"})
    assert runner.show_status(tmp_path) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["status"] == "INTERRUPTED" and status["lock_owner"] == "idle"


def test_gate_stage_has_no_wall_clock_budget_and_defers_workspace_setup(tmp_path, monkeypatch):
    import pipeline.functional_fixer as functional
    import pipeline.gate_bridge as bridge
    calls = {}
    class Loop:
        def __init__(self, **kwargs):
            calls["limits"] = kwargs
        def run(self, *args, **kwargs):
            calls["run"] = kwargs
            return {"success": True, "rounds": 1}
    monkeypatch.setattr(functional, "FunctionalFixLoop", Loop)
    monkeypatch.setattr(bridge, "prepare_workspace", lambda *a, **kw: pytest.fail("loop owns setup"))
    monkeypatch.setenv("DIFF_GATE_TIME_BUDGET_S", "1")
    args = main.parse_args(["--max-fix-rounds", "6", "--max-gate-rounds", "4"])
    assert main.run_diff_gate_stage(tmp_path, None, args)
    assert calls["limits"] == {"max_gate_rounds": 4, "build_fix_rounds": 6}
    assert "time_budget_s" not in calls["run"]


def test_model_call_limit_defaults_to_unlimited_and_zero_is_unlimited():
    assert main.parse_args([]).max_model_calls is None
    assert main.parse_args(["--max-model-calls", "0"]).max_model_calls is None
    assert main.parse_args(["--max-model-calls", "25"]).max_model_calls == 25

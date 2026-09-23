"""Structural contract tests use small synthetic apps, never a specific sample."""
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline.artifacts import (
    ArtifactContractError, build_translation_manifest, load_artifact_manifest,
    output_role, project_source_fingerprint, stable_unit_id,
    validate_project_contract, write_artifact_manifest,
)
from pipeline.order_determiner import Unit
from pipeline.project_packager import _choose_entry_page, _register_pages, package_project
from pipeline.resource_migrator import parse_manifest
from pipeline.unit_translator import TranslationResult


def make_project(tmp_path, *, code="@Entry\n@Component\nstruct Screen { build() {} }"):
    source = "java/example/Start.java"
    result = TranslationResult(unit_name="screen", file_name="Renamed.ets", code=code,
                               success=True, sources=[source])
    data = build_translation_manifest(tmp_path / "android", [Unit("screen", [source])], [result],
        {source: SimpleNamespace(class_name="Start", package="example")},
        {"activities": [{"name": "example.Start"}],
         "launchers": [{"activity": "example.Start", "component": "example.LaunchAlias"}]})
    root = tmp_path / "harmony"
    path = root / data["outputs"][0]["path"]
    path.parent.mkdir(parents=True)
    path.write_text(code, encoding="utf-8", newline="")
    write_artifact_manifest(root, data)
    return root, data


def test_manifest_maps_actual_renamed_output_and_launcher_alias(tmp_path):
    root, data = make_project(tmp_path)
    assert data["launcher"]["route"] == "pages/Renamed"
    assert data["pages"][0]["output"].endswith("Renamed.ets")
    assert validate_project_contract(root) == []
    assert stable_unit_id(["a", "b"]) == stable_unit_id(["b", "a"])


def test_android_manifest_extracts_alias_main_launcher_together(tmp_path):
    path = tmp_path / "AndroidManifest.xml"
    path.write_text('''<manifest xmlns:android="http://schemas.android.com/apk/res/android" package="example">
      <application><activity android:name=".Start"/>
      <activity-alias android:name=".Launch" android:targetActivity=".Start">
      <intent-filter><action android:name="android.intent.action.MAIN"/>
      <category android:name="android.intent.category.LAUNCHER"/></intent-filter></activity-alias>
      </application></manifest>''', encoding="utf-8")
    result = parse_manifest(str(path))
    assert result["launchers"] == [{"component": "example.Launch", "activity": "example.Start"}]
    assert result["activities"][0]["name"] == "example.Start"


def test_no_entry_guessing_and_comment_is_not_page(tmp_path):
    assert output_role('// @Entry\nconst text = "@Entry";\n@Component\nstruct X {}') == "component"
    with pytest.raises(ArtifactContractError):
        _choose_entry_page(["pages/MainPage", "pages/Wallpaper"], tmp_path / "missing.json")


@pytest.mark.parametrize("failure", ["missing_file", "missing_launcher", "wrong_role", "duplicate_route"])
def test_invalid_contract_blocks_packaging_before_copy(tmp_path, failure):
    root, data = make_project(tmp_path)
    if failure == "missing_file":
        (root / data["outputs"][0]["path"]).unlink()
    elif failure == "missing_launcher":
        data["launcher"] = None
    elif failure == "wrong_role":
        (root / data["outputs"][0]["path"]).write_text("@Component\nstruct Screen {}", encoding="utf-8")
    else:
        data["outputs"].append(dict(data["outputs"][0]))
    write_artifact_manifest(root, data)
    template = tmp_path / "template"
    template.mkdir()
    output = tmp_path / "packaged"
    with pytest.raises(ArtifactContractError):
        package_project(template, root, output)
    assert not output.exists()


def test_dangling_navigation_is_contract_failure(tmp_path):
    root, _ = make_project(tmp_path, code="@Entry\n@Component\nstruct Screen { open() { router.pushUrl({ url: 'pages/Missing' }) } build() {} }")
    assert "CONTRACT_NAVIGATION" in {issue["code"] for issue in validate_project_contract(root)}


def test_packaged_route_and_ability_are_both_checked(tmp_path):
    root, data = make_project(tmp_path)
    main = root / "entry/src/main"
    (main / "module.json5").write_text("{}", encoding="utf-8")
    ability = main / "ets/entryability/EntryAbility.ets"
    ability.parent.mkdir(parents=True)
    ability.write_text("windowStage.loadContent('pages/Wrong');", encoding="utf-8")
    _register_pages(main, main / "unused.json", data)
    assert validate_project_contract(root) == []
    ability.write_text("windowStage.loadContent('pages/Wrong');", encoding="utf-8")
    assert "CONTRACT_ENTRY" in {issue["code"] for issue in validate_project_contract(root)}


def test_path_escape_is_rejected(tmp_path):
    root, data = make_project(tmp_path)
    data["outputs"][0]["path"] = "../escape.ets"
    write_artifact_manifest(root, data)
    with pytest.raises(ArtifactContractError):
        load_artifact_manifest(root)


def test_build_fingerprint_ignores_output_and_tracks_inputs(tmp_path):
    root, data = make_project(tmp_path)
    baseline = project_source_fingerprint(root)
    (root / ".pipeline_build.json").write_text('{"status":"success"}', encoding="utf-8")
    source = root / data["outputs"][0]["path"]
    source.with_suffix(source.suffix + ".funcbak").write_text(
        "repair backup", encoding="utf-8"
    )
    (source.parent / ".run-tx-interrupted-write").write_bytes(b"temporary")
    output = root / "entry/build/out.hap"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"old build")
    assert project_source_fingerprint(root) == baseline
    source.write_text("changed", encoding="utf-8")
    assert project_source_fingerprint(root) != baseline


@pytest.mark.parametrize("relative", [
    "tests/case.json",
    "seeds/input.bin",
    "logs/runtime.bin",
    "transactions/state.json",
    "candidates/proposal.json",
    ".pipeline_transactions/recovery/journal.json",
    "oracle.py",
    "oracle.json",
    "page_pairs.json",
    "unit_page_map.json",
    ".env.fixture",
    ".pipeline_build.json",
    "capture.log",
    "payload.tmp",
    "write.lock",
    "package.hap",
    "package.hsp",
])
def test_nested_resource_names_are_build_inputs(tmp_path, relative):
    root, _ = make_project(tmp_path)
    baseline = project_source_fingerprint(root)
    resource = root / "entry/src/main/resources/rawfile" / relative
    resource.parent.mkdir(parents=True, exist_ok=True)
    resource.write_bytes(b"packaged resource")
    assert project_source_fingerprint(root) != baseline


def test_root_tool_state_and_nested_build_caches_are_not_build_inputs(tmp_path):
    root, _ = make_project(tmp_path)
    baseline = project_source_fingerprint(root)
    ignored = {
        ".pipeline_transactions/build/journal.json": b"transaction",
        "tests/case.json": b"gate test",
        "logs/gate.log": b"gate log",
        "oracle.json": b"{}",
        "page_pairs.json": b"{}",
        "unit_page_map.json": b"{}",
        ".env.fixture": b"secret=value",
        "gate.log": b"gate log",
        "payload.tmp": b"temporary",
        "pipeline.lock": b"lock",
        "old-package.hap": b"artifact",
        "old-package.hsp": b"artifact",
        "entry/build/default/outputs/app.hap": b"artifact",
        "entry/.hvigor/cache/state.json": b"cache",
        "entry/oh_modules/pkg/index.js": b"dependency",
        "entry/node_modules/pkg/index.js": b"dependency",
    }
    for relative, contents in ignored.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
    assert project_source_fingerprint(root) == baseline


def test_build_metadata_uses_absolute_project_and_current_inputs(tmp_path, monkeypatch):
    import pipeline.project_packager as module
    from run_control import Budget, budget_scope
    root, _ = make_project(tmp_path)
    (root / "hvigorw.bat").write_text("wrapper", encoding="utf-8")
    calls = []

    def build(command, **options):
        calls.append((command, options))
        hap = root / "entry/build/default/outputs/default/entry-default-signed.hap"
        hap.parent.mkdir(parents=True)
        hap.write_bytes(b"new-build")
        return subprocess.CompletedProcess(command, 0, "BUILD SUCCESSFUL", "")

    monkeypatch.setattr(module, "run_process", build)
    budget = Budget(max_builds=1)
    with budget_scope(budget):
        result = module.run_hvigor(root)
    metadata = json.loads((root / ".pipeline_build.json").read_text(encoding="utf-8"))
    assert result.returncode == 0
    assert budget.counts["builds"] == 1
    assert metadata["status"] == "success"
    assert metadata["project_input_sha256"] == project_source_fingerprint(root)
    assert metadata["artifacts"][0]["signed"] is True
    assert calls[0][1]["cwd"].is_absolute()
    assert 0 < calls[0][1]["timeout"] <= 600


def test_failed_build_overwrites_old_success_metadata(tmp_path, monkeypatch):
    import pipeline.project_packager as module
    root, _ = make_project(tmp_path)
    (root / "hvigorw.bat").write_text("wrapper", encoding="utf-8")
    (root / ".pipeline_build.json").write_text('{"status":"success","artifacts":["old.hap"]}', encoding="utf-8")
    from run_control import BudgetExceeded
    monkeypatch.setattr(module, "run_process", lambda *args, **kwargs: (_ for _ in ()).throw(BudgetExceeded("process_timeout")))
    with pytest.raises(BudgetExceeded):
        module.run_hvigor(root)
    metadata = json.loads((root / ".pipeline_build.json").read_text(encoding="utf-8"))
    assert metadata["status"] == "failed" and metadata["artifacts"] == []
    assert metadata["stop_reason"] == "process_timeout"


def test_build_budget_exhaustion_records_failure_before_launch(tmp_path, monkeypatch):
    import pipeline.project_packager as module
    from run_control import Budget, BudgetExceeded, budget_scope
    root, _ = make_project(tmp_path)
    (root / "hvigorw.bat").write_text("wrapper", encoding="utf-8")
    (root / ".pipeline_build.json").write_text('{"status":"success","artifacts":[]}', encoding="utf-8")
    monkeypatch.setattr(module, "run_process", lambda *a, **kw: pytest.fail("Must not spawn a process"))
    with budget_scope(Budget(max_builds=0)):
        with pytest.raises(BudgetExceeded):
            module.run_hvigor(root)
    metadata = json.loads((root / ".pipeline_build.json").read_text(encoding="utf-8"))
    assert metadata["status"] == "failed" and metadata["stop_reason"] == "max_builds"
    assert metadata["artifacts"] == []


def test_build_success_without_hap_is_failure(tmp_path, monkeypatch):
    import pipeline.project_packager as module
    root, _ = make_project(tmp_path)
    (root / "hvigorw.bat").write_text("wrapper", encoding="utf-8")
    monkeypatch.setattr(module, "run_process", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "success", ""))
    assert module.run_hvigor(root).returncode != 0


def test_contract_hash_and_unit_ownership_are_checked(tmp_path):
    root, data = make_project(tmp_path)
    output = root / data["outputs"][0]["path"]
    output.write_bytes(output.read_bytes() + b"\n// changed")
    assert "CONTRACT_OUTPUT_HASH" in {issue["code"] for issue in validate_project_contract(root)}
    data["outputs"][0]["unit_id"] = "missing_owner"
    write_artifact_manifest(root, data)
    assert "CONTRACT_OWNERSHIP" in {issue["code"] for issue in validate_project_contract(root)}


@pytest.mark.parametrize("field,value", [("units", [None]), ("outputs", [{"path": None}]),
                                         ("required_activities", [[]]), ("launcher", {"route": []}),
                                         ("pages", [{"android_activity": []}])])
def test_malformed_contract_returns_diagnostics_instead_of_crashing(tmp_path, field, value):
    root, data = make_project(tmp_path)
    data[field] = value
    write_artifact_manifest(root, data)
    issues = validate_project_contract(root)
    assert issues and issues[0]["code"] == "CONTRACT_MANIFEST"


def test_raw_resource_changes_invalidate_build_provenance(tmp_path):
    root, _ = make_project(tmp_path)
    resource = root / "entry/src/main/resources/rawfile/weights.custom"
    resource.parent.mkdir(parents=True)
    resource.write_bytes(b"old")
    previous = project_source_fingerprint(root)
    resource.write_bytes(b"new")
    assert project_source_fingerprint(root) != previous


def test_old_signed_hap_is_not_mistaken_for_current_unsigned_build(tmp_path, monkeypatch):
    import pipeline.project_packager as module
    root, _ = make_project(tmp_path)
    (root / "hvigorw.bat").write_text("wrapper", encoding="utf-8")
    directory = root / "entry/build/default/outputs/default"
    directory.mkdir(parents=True)
    old = directory / "entry-default-signed.hap"
    old.write_bytes(b"stale signed")
    os.utime(old, (1000, 1000))

    def build(command, **options):
        (directory / "entry-default-unsigned.hap").write_bytes(b"fresh unsigned")
        return subprocess.CompletedProcess(command, 0, "success", "")

    monkeypatch.setattr(module, "run_process", build)
    assert module.run_hvigor(root).returncode == 0
    metadata = json.loads((root / ".pipeline_build.json").read_text(encoding="utf-8"))
    assert len(metadata["artifacts"]) == 1
    assert metadata["artifacts"][0]["path"].endswith("unsigned.hap")


def test_untracked_generated_file_cannot_silently_join_package(tmp_path):
    root, _ = make_project(tmp_path)
    (root / "entry/src/main/ets/pages/Stale.ets").write_text("export class Stale {}", encoding="utf-8")
    assert "CONTRACT_UNTRACKED_OUTPUT" in {issue["code"] for issue in validate_project_contract(root)}

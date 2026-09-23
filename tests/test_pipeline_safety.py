"""翻译流水线产物隔离、资源去重和错误解析测试。"""

from pathlib import Path

import pytest

from pipeline.build_fixer import BuildFixLoop, ErrorParser
from pipeline.artifacts import validate_project_contract
from pipeline.project_packager import (
    InvalidResourceNameError,
    ResourceConflictError,
    find_component_new_violations,
    find_invalid_resource_names,
    find_resource_name_conflicts,
    validate_resource_names,
)
from pipeline.resource_migrator import ResourceMigrator, reset_generated_artifacts


def test_reset_generated_artifacts_only_removes_pipeline_outputs(tmp_path: Path):
    generated = tmp_path / "HarmonyProject"
    page = generated / "entry/src/main/ets/pages/OldPage.ets"
    resource = generated / "entry/src/main/resources/base/media/old.png"
    mapping = generated / ".resource_mapping.json"
    preserved = generated / "keep.txt"
    for path in (page, resource, mapping, preserved):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("old", encoding="utf-8")

    removed = reset_generated_artifacts(generated)

    assert len(removed) == 3
    assert not page.exists()
    assert not resource.exists()
    assert not mapping.exists()
    assert preserved.read_text(encoding="utf-8") == "old"


def test_resource_migrator_deduplicates_same_logical_media_name(tmp_path: Path):
    android = tmp_path / "android"
    hdpi = android / "app/src/main/res/mipmap-hdpi"
    xxxhdpi = android / "app/src/main/res/mipmap-xxxhdpi"
    hdpi.mkdir(parents=True)
    xxxhdpi.mkdir(parents=True)
    (hdpi / "ic_launcher.webp").write_bytes(b"large-but-lower-density")
    (xxxhdpi / "ic_launcher.9.png").write_bytes(b"x")

    harmony = tmp_path / "HarmonyProject"
    stale = harmony / "entry/src/main/resources/base/media/ic_launcher.jpg"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"stale")

    mapping = ResourceMigrator(str(android), str(harmony)).run()

    media = harmony / "entry/src/main/resources/base/media"
    launchers = [path for path in media.iterdir() if path.stem == "ic_launcher"]
    assert [path.name for path in launchers] == ["ic_launcher.png"]
    entry = next(item for item in mapping.entries if item.android_ref == "R.mipmap.ic_launcher")
    assert Path(entry.harmony_path).name == "ic_launcher.png"


def test_resource_conflict_preflight_groups_by_directory_and_stem(tmp_path: Path):
    resources = tmp_path / "resources"
    base_media = resources / "base/media"
    dark_media = resources / "dark/media"
    base_media.mkdir(parents=True)
    dark_media.mkdir(parents=True)
    (base_media / "icon.png").write_bytes(b"png")
    (base_media / "icon.webp").write_bytes(b"webp")
    (dark_media / "icon.png").write_bytes(b"variant")

    conflicts = find_resource_name_conflicts(resources)

    assert len(conflicts) == 1
    assert conflicts[0]["directory"] == "base/media"
    assert {path.name for path in conflicts[0]["files"]} == {"icon.png", "icon.webp"}
    with pytest.raises(ResourceConflictError, match="base/media/icon"):
        validate_resource_names(resources)


def test_resource_preflight_rejects_invalid_logical_name(tmp_path: Path):
    resources = tmp_path / "resources"
    media = resources / "base/media"
    media.mkdir(parents=True)
    invalid = media / "icon.9.png"
    invalid.write_bytes(b"nine-patch")

    assert find_invalid_resource_names(resources) == [{
        "name": "icon.9",
        "file": Path("base/media/icon.9.png"),
    }]
    with pytest.raises(InvalidResourceNameError, match="icon.9.png"):
        validate_resource_names(resources)


def test_error_parser_recognizes_harmony_resource_conflict():
    stderr = """
Error Code: 11211117
Error: Resource Pack Error
Error Message: Resource 'ic_launcher' conflict. It is first declared at 'E:\\p\\ic_launcher.webp' and declared again at 'E:\\p\\ic_launcher.png'.
"""

    errors = ErrorParser.parse("", stderr)

    assert errors == [{
        "file": r"E:\p\ic_launcher.png",
        "line": 1,
        "column": 1,
        "message": r"Resource 'ic_launcher' conflicts with 'E:\p\ic_launcher.webp'",
        "code": "RESOURCE_CONFLICT",
    }]


def test_error_parser_recognizes_invalid_resource_name():
    stderr = """
Error Message: Invalid resource name 'icon.9'. It should match the pattern [a-zA-Z0-9_]. At file: E:\\p\\icon.9.png
"""

    errors = ErrorParser.parse("", stderr)

    assert errors == [{
        "file": r"E:\p\icon.9.png",
        "line": 1,
        "column": 1,
        "message": "Invalid resource name 'icon.9'",
        "code": "RESOURCE_INVALID_NAME",
    }]


def test_build_fix_loop_stops_before_hvigor_on_resource_conflict(tmp_path: Path):
    media = tmp_path / "entry/src/main/resources/base/media"
    media.mkdir(parents=True)
    (media / "icon.png").write_bytes(b"png")
    (media / "icon.webp").write_bytes(b"webp")

    result = BuildFixLoop(llm=object()).run(tmp_path)

    assert result["success"] is False
    assert result["builds"] == 0
    assert result["initial_errors_count"] == 1
    assert result["remaining_errors"][0]["code"] == "RESOURCE_CONFLICT"


def test_find_component_new_violations(tmp_path: Path):
    pages = tmp_path / "entry/src/main/ets/pages"
    pages.mkdir(parents=True)
    # 组件声明在一个文件，new 违规在另一个文件（跨文件收集组件名）
    (pages / "RainAdapter.ets").write_text(
        "@Component\nexport struct RainAdapter {\n  build() {}\n}\n",
        encoding="utf-8")
    (pages / "MainPage.ets").write_text(
        "import { RainAdapter } from './RainAdapter';\n"
        "@Entry\n@Component\nexport struct MainPage {\n"
        "  aboutToAppear(): void {\n"
        "    this.x = new RainAdapter({ rainDataList: [] });\n"
        "  }\n"
        "  build() { RainAdapter() }\n"   # 声明式使用不算违规
        "}\n",
        encoding="utf-8")
    # 普通类的 new 不受影响
    (pages / "DataModel.ets").write_text(
        "export class DataModel {}\nconst d = new DataModel();\n",
        encoding="utf-8")

    violations = find_component_new_violations(tmp_path)

    assert violations == [{
        "file": "entry/src/main/ets/pages/MainPage.ets",
        "line": 6,
        "component": "RainAdapter",
    }]


def test_find_component_new_violations_empty_project(tmp_path: Path):
    assert find_component_new_violations(tmp_path) == []


def test_build_fix_context_includes_bounded_relative_dependency(tmp_path: Path):
    pages = tmp_path / "entry/src/main/ets/pages"
    pages.mkdir(parents=True)
    caller = pages / "Caller.ets"
    caller.write_text("import { Api } from './Api';\nexport class Caller {}\n", encoding="utf-8")
    dependency = pages / "Api.ets"
    dependency.write_text("export class Api { static call(value: number): void {} }\n", encoding="utf-8")

    context = BuildFixLoop._local_dependency_context(caller, tmp_path)

    assert "entry/src/main/ets/pages/Api.ets" in context
    assert "static call(value: number)" in context


@pytest.mark.parametrize("write_fails", [False, True])
def test_build_repair_replaces_complete_file_and_keeps_original_backup(tmp_path, monkeypatch, write_fails):
    import run_control
    root = tmp_path / "project"
    source = root / "entry/src/main/ets/pages/Main.ets"
    source.parent.mkdir(parents=True)
    original = "class Main { value(): number { return 1; } }\r\n"
    fixed = "class Main { value(): number { return 2; } }\n"
    source.write_bytes(original.encode("utf-8"))
    loop = BuildFixLoop(llm=object())
    monkeypatch.setattr(loop, "_reflection_fix", lambda *a, **kw: fixed)
    replace = run_control.os.replace
    def replace_or_interrupt(src, dst):
        if write_fails and Path(dst) == source:
            raise OSError("interrupted before source replacement")
        return replace(src, dst)
    monkeypatch.setattr(run_control.os, "replace", replace_or_interrupt)
    errors = [{"file": str(source), "line": 1, "message": "type error", "code": "TYPE_ERROR"}]
    if write_fails:
        with pytest.raises(OSError, match="interrupted"):
            loop._fix_files(root, None, errors)
    else:
        loop._fix_files(root, None, errors)
    assert source.read_bytes() == (original if write_fails else fixed).encode("utf-8")
    backup = source.with_suffix(".ets.bak")
    if write_fails:
        assert not backup.exists()
    else:
        assert backup.read_bytes() == original.encode("utf-8")
    assert not list(source.parent.glob(".run-tmp-*"))


def test_build_repair_updates_sync_source_and_both_manifests(tmp_path, monkeypatch):
    from test_artifact_contract import make_project

    packaged, _ = make_project(tmp_path / "packaged")
    generated, _ = make_project(tmp_path / "generated")
    source = packaged / "entry/src/main/ets/pages/Renamed.ets"
    sync_source = generated / "entry/src/main/ets/pages/Renamed.ets"
    fixed = source.read_text(encoding="utf-8").replace(
        "build() {}", "build() { Text('fixed') }"
    )
    loop = BuildFixLoop(llm=object())
    monkeypatch.setattr(loop, "_reflection_fix", lambda *a, **kw: fixed)

    loop._fix_files(packaged, generated, [{
        "file": str(source), "line": 1, "message": "type error",
        "code": "TYPE_ERROR",
    }])

    assert source.read_text(encoding="utf-8") == fixed
    assert sync_source.read_text(encoding="utf-8") == fixed
    assert validate_project_contract(packaged) == []
    assert validate_project_contract(generated) == []


def test_build_repair_sync_failure_rolls_back_both_projects(tmp_path, monkeypatch):
    import run_control
    from test_artifact_contract import make_project

    packaged, _ = make_project(tmp_path / "packaged")
    generated, _ = make_project(tmp_path / "generated")
    source = packaged / "entry/src/main/ets/pages/Renamed.ets"
    sync_source = generated / "entry/src/main/ets/pages/Renamed.ets"
    before = source.read_bytes()
    sync_before = sync_source.read_bytes()
    packaged_manifest = (packaged / "translation_manifest.json").read_bytes()
    generated_manifest = (generated / "translation_manifest.json").read_bytes()
    fixed = source.read_text(encoding="utf-8").replace(
        "build() {}", "build() { Text('fixed') }"
    )
    loop = BuildFixLoop(llm=object())
    monkeypatch.setattr(loop, "_reflection_fix", lambda *a, **kw: fixed)
    real_atomic_write = run_control.atomic_write

    def fail_sync(path, data):
        if Path(path).resolve() == sync_source.resolve():
            raise OSError("injected sync failure")
        return real_atomic_write(path, data)

    monkeypatch.setattr(run_control, "atomic_write", fail_sync)

    with pytest.raises(OSError, match="injected sync failure"):
        loop._fix_files(packaged, generated, [{
            "file": str(source), "line": 1, "message": "type error",
            "code": "TYPE_ERROR",
        }])

    assert source.read_bytes() == before
    assert sync_source.read_bytes() == sync_before
    assert (packaged / "translation_manifest.json").read_bytes() == packaged_manifest
    assert (generated / "translation_manifest.json").read_bytes() == generated_manifest


def test_build_interrupt_is_not_masked_by_manifest_refresh(tmp_path, monkeypatch):
    from unittest.mock import Mock

    from run_control import BudgetExceeded

    loop = BuildFixLoop(llm=object())
    monkeypatch.setattr(
        loop, "_run_in_place",
        Mock(side_effect=BudgetExceeded("build_budget")),
    )
    refresh = Mock(side_effect=OSError("manifest failure"))
    monkeypatch.setattr(loop, "_refresh_manifests", refresh)

    with pytest.raises(BudgetExceeded, match="build_budget"):
        loop.run(tmp_path)

    refresh.assert_not_called()

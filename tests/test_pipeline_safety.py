"""翻译流水线产物隔离、资源去重和错误解析测试。"""

from pathlib import Path

import pytest

from pipeline.build_fixer import BuildFixLoop, ErrorParser
from pipeline.project_packager import (
    InvalidResourceNameError,
    ResourceConflictError,
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

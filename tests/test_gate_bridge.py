"""gate_bridge / functional_fixer 离线单元测试（不依赖设备与 LLM 服务）。"""
import json

import pytest

from test_artifact_contract import make_project
from pipeline.artifacts import ArtifactContractError, validate_project_contract, write_artifact_manifest
from pipeline.functional_fixer import FunctionalFixLoop
from pipeline.gate_bridge import (
    build_page_pairs,
    build_unit_page_map,
    find_hap,
    page_for_activity,
    prepare_workspace,
    prepare_static_index,
    read_bundle_name,
    unit_ets_files,
)

PLAN = {
    "units": [
        {"name": "Unit_Main", "sources": [
            "app/src/main/java/com/example/app/MainActivity.java",
            "app/src/main/res/layout/activity_main.xml",
        ]},
        {"name": "Unit_Profile", "sources": [
            "app/src/main/java/com/example/app/ProfileActivity.java",
            "app/src/main/java/com/example/app/ProfileHelper.java",
        ]},
        {"name": "Unit_Utils", "sources": [
            "app/src/main/java/com/example/app/Utils.java",
        ]},
    ],
}


@pytest.fixture
def plan_file(tmp_path):
    p = tmp_path / "translation_plan.json"
    p.write_text(json.dumps(PLAN, ensure_ascii=False), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# 契约文件生成
# ---------------------------------------------------------------------------

def test_page_for_activity_naming():
    assert page_for_activity("MainActivity") == "MainPage"
    assert page_for_activity("EditNameActivity") == "EditNamePage"


def test_build_unit_page_map(plan_file, tmp_path):
    out = tmp_path / "unit_page_map.json"
    mapping = build_unit_page_map(plan_file, out)

    assert mapping["Unit_Main"]["android_pages"] == ["MainActivity"]
    assert mapping["Unit_Main"]["harmony_pages"] == ["EntryAbility:pages/MainPage"]
    # 非 Activity 文件不产生页面
    assert mapping["Unit_Utils"]["android_pages"] == []
    # 全局条目
    assert mapping["__global__"]["android_pages"] == ["*"]
    # 已落盘
    assert json.loads(out.read_text(encoding="utf-8")) == mapping


def test_build_page_pairs_key_forms(plan_file, tmp_path):
    out = tmp_path / "page_pairs.json"
    pairs = build_page_pairs(plan_file, out)

    expected = "EntryAbility:pages/MainPage"
    assert pairs["MainActivity"] == expected
    assert pairs[".MainActivity"] == expected
    assert pairs["com.example.app.MainActivity"] == expected


def test_build_page_pairs_preserves_manual_entries(plan_file, tmp_path):
    out = tmp_path / "page_pairs.json"
    out.write_text(json.dumps({
        "MainActivity": "EntryAbility:pages/CustomPage",   # 人工修正
        "ExtraActivity": "EntryAbility:pages/ExtraPage",   # 人工新增
    }), encoding="utf-8")

    pairs = build_page_pairs(plan_file, out)
    assert pairs["MainActivity"] == "EntryAbility:pages/CustomPage"
    assert pairs["ExtraActivity"] == "EntryAbility:pages/ExtraPage"
    assert pairs[".ProfileActivity"] == "EntryAbility:pages/ProfilePage"


def test_prepare_real_mapping_allows_entry_and_navigation_defects(tmp_path):
    root, data = make_project(tmp_path, code=(
        "@Entry\n@Component\nstruct Screen {\n"
        "  open() { router.pushUrl({ url: 'pages/Missing' }) }\n"
        "  build() { Text('weather') }\n}\n"))
    main = root / "entry/src/main"
    (main / "module.json5").write_text("{}", encoding="utf-8")
    ability = main / "ets/entryability/EntryAbility.ets"
    ability.parent.mkdir()
    ability.write_text("windowStage.loadContent('pages/Wrong');", encoding="utf-8")
    registration = main / "resources/base/profile/main_pages.json"
    registration.parent.mkdir(parents=True)
    registration.write_text(json.dumps({"src": ["pages/Renamed"]}), encoding="utf-8")
    issues = {issue["code"] for issue in validate_project_contract(root)}
    assert {"CONTRACT_ENTRY", "CONTRACT_NAVIGATION"} <= issues
    original = ability.read_bytes()
    workspace = tmp_path / "gate"

    prepare_workspace(workspace, None, project_dir=root)

    pairs = json.loads((workspace / "page_pairs.json").read_text(encoding="utf-8"))
    mapping = json.loads((workspace / "unit_page_map.json").read_text(encoding="utf-8"))
    assert pairs["example.Start"] == "EntryAbility:pages/Renamed"
    assert mapping["screen"]["unit_id"] == data["units"][0]["unit_id"]
    assert ability.read_bytes() == original


def test_prepare_static_index_persists_index_and_blocks_dangling_route(tmp_path):
    root, _ = make_project(
        tmp_path,
        code=("@Entry\n@Component\nstruct Screen {\n"
              "  open() { router.pushUrl({ url: 'pages/Missing' }) }\n"
              "  build() {}\n}\n"),
    )
    workspace = tmp_path / "gate"

    with pytest.raises(RuntimeError, match="STATIC_CONTRACT_INVALID"):
        prepare_static_index(root, workspace)

    assert (workspace / "arkts_index.json").is_file()
    issues = json.loads((workspace / "arkts_static_issues.json").read_text())
    assert any(item["code"] == "UNREGISTERED_ROUTE" for item in issues["issues"])


@pytest.mark.parametrize("failure", ["missing", "escape"])
def test_prepare_rejects_missing_or_escaping_page_output(tmp_path, failure):
    root, data = make_project(tmp_path)
    if failure == "missing":
        (root / data["pages"][0]["output"]).unlink()
    else:
        (tmp_path / "outside.ets").write_text("@Entry struct Outside {}", encoding="utf-8")
        data["pages"][0]["output"] = "../outside.ets"
        write_artifact_manifest(root, data)
    workspace = tmp_path / "gate"

    with pytest.raises((RuntimeError, ArtifactContractError),
                       match="PAGE_MAPPING_INVALID|Unsafe artifact path"):
        prepare_workspace(workspace, None, project_dir=root)

    assert not (workspace / "page_pairs.json").exists()
    assert not (workspace / "unit_page_map.json").exists()


def test_prepare_legacy_mapping_reuses_explicit_files_unchanged(tmp_path):
    root = tmp_path / "legacy"
    page = root / "entry/src/main/ets/pages/Actual.ets"
    page.parent.mkdir(parents=True)
    page.write_text("@Entry struct Actual {}", encoding="utf-8")
    workspace = tmp_path / "gate"
    workspace.mkdir()
    originals = {
        "page_pairs.json": b'{ "example.Start": "EntryAbility:pages/Actual" }\n',
        "unit_page_map.json": (b'{ "screen": {"android_pages":["example.Start"],'
                               b'"harmony_pages":["EntryAbility:pages/Actual"]} }\n'),
    }
    for name, content in originals.items():
        (workspace / name).write_bytes(content)
    stale_plan = tmp_path / "plan.json"
    stale_plan.write_text("broken stale plan", encoding="utf-8")

    prepare_workspace(workspace, stale_plan, project_dir=root)

    assert {name: (workspace / name).read_bytes() for name in originals} == originals


def test_prepare_legacy_plan_alone_does_not_guess_page_mapping(tmp_path, plan_file):
    root = tmp_path / "legacy"
    root.mkdir()
    workspace = tmp_path / "gate"

    with pytest.raises(RuntimeError, match="PAGE_MAPPING_MISSING"):
        prepare_workspace(workspace, plan_file, project_dir=root)

    assert not (workspace / "page_pairs.json").exists()
    assert not (workspace / "unit_page_map.json").exists()


@pytest.mark.parametrize("conflicting_file", ["page_pairs.json", "unit_page_map.json"])
def test_prepare_conflicting_mapping_stops_without_overwriting_files(tmp_path, conflicting_file):
    root, _ = make_project(tmp_path)
    workspace = tmp_path / "gate"
    prepare_workspace(workspace, None, project_dir=root)
    path = workspace / conflicting_file
    data = json.loads(path.read_text(encoding="utf-8"))
    if conflicting_file == "page_pairs.json":
        data["example.Start"] = "EntryAbility:pages/ManualChoice"
    else:
        data["screen"]["harmony_pages"] = ["EntryAbility:pages/ManualChoice"]
    path.write_text(json.dumps(data), encoding="utf-8")
    originals = {name: (workspace / name).read_bytes()
                 for name in ("page_pairs.json", "unit_page_map.json")}

    with pytest.raises(RuntimeError, match="PAGE_MAPPING_CONFLICT"):
        prepare_workspace(workspace, None, project_dir=root)

    assert {name: (workspace / name).read_bytes() for name in originals} == originals


# ---------------------------------------------------------------------------
# 工程侧信息
# ---------------------------------------------------------------------------

def test_read_bundle_name(tmp_path):
    app_dir = tmp_path / "AppScope"
    app_dir.mkdir()
    (app_dir / "app.json5").write_text(
        '{\n  "app": {\n    "bundleName": "com.example.hm",\n'
        '    "vendor": "demo"\n  }\n}\n',
        encoding="utf-8",
    )
    assert read_bundle_name(tmp_path) == "com.example.hm"
    assert read_bundle_name(tmp_path / "nonexistent") is None


def test_find_hap_prefers_signed(tmp_path):
    out_dir = tmp_path / "entry" / "build" / "default" / "outputs" / "default"
    out_dir.mkdir(parents=True)
    (out_dir / "entry-default-unsigned.hap").write_bytes(b"u")
    (out_dir / "entry-default-signed.hap").write_bytes(b"s")

    hap = find_hap(tmp_path)
    assert hap is not None and hap.name == "entry-default-signed.hap"
    assert find_hap(tmp_path / "nonexistent") is None


def test_unit_ets_files(tmp_path):
    pages = tmp_path / "entry" / "src" / "main" / "ets" / "pages"
    pages.mkdir(parents=True)
    for name in ("MainPage.ets", "ProfilePage.ets", "ProfileHelper.ets"):
        (pages / name).write_text("// ets", encoding="utf-8")

    files = unit_ets_files(["Unit_Profile"], PLAN, tmp_path)
    assert [f.name for f in files] == ["ProfilePage.ets", "ProfileHelper.ets"]

    assert unit_ets_files(["Unit_Unknown"], PLAN, tmp_path) == []


# ---------------------------------------------------------------------------
# functional_fixer（不触网）
# ---------------------------------------------------------------------------

class FakeLLM:
    """返回固定修复代码的假 LLM。"""

    def __init__(self, response: str):
        self.response = response
        self.calls: list[list] = []

    def invoke(self, messages):
        self.calls.append(messages)
        return self.response


def test_fix_file_proposes_before_batch_commit_writes_backup_and_diff(tmp_path):
    fp = tmp_path / "MainPage.ets"
    source = ("@Entry\n@Component\nstruct MainPage {\n"
              "  build() {\n    Text('旧')\n  }\n}\n")
    fp.write_text(source, encoding="utf-8")

    fixed_code = ("@Entry\n@Component\nstruct MainPage {\n"
                  "  build() {\n    Text('新')\n  }\n}\n")
    llm = FakeLLM(f"分析：绑定丢失。\n```typescript\n{fixed_code}```\n")
    loop = FunctionalFixLoop(llm=llm, max_gate_rounds=1)

    diff = loop._fix_file(fp, "## 报告", "")
    assert diff and "-    Text('旧')" in diff and "+    Text('新')" in diff
    assert fp.read_text(encoding="utf-8") == source
    assert not fp.with_suffix(".ets.funcbak").exists()

    loop._commit_file_repairs(
        tmp_path.resolve(), None, None,
        [(fp.resolve(), loop._last_file_repair_outcome)],
    )

    assert fp.read_text(encoding="utf-8").rstrip() == fixed_code.rstrip()
    # 首次修复保留原文备份
    backup = fp.with_suffix(".ets.funcbak")
    assert backup.exists() and backup.read_text(encoding="utf-8") == source


def test_fix_file_rejects_irrelevant_and_degenerate(tmp_path):
    fp = tmp_path / "MainPage.ets"
    source = "@Entry\nstruct MainPage {\n  build() {}\n}\n"
    fp.write_text(source, encoding="utf-8")

    # LLM 判断与本文件无关 → 不修改
    loop = FunctionalFixLoop(llm=FakeLLM("与本文件无关"), max_gate_rounds=1)
    assert loop._fix_file(fp, "## 报告", "") is None
    assert fp.read_text(encoding="utf-8") == source

    # 退化输出（无 ArkTS 结构关键字）→ 丢弃
    loop2 = FunctionalFixLoop(
        llm=FakeLLM("```typescript\nlet x = 1\n```"), max_gate_rounds=1)
    assert loop2._fix_file(fp, "## 报告", "") is None
    assert fp.read_text(encoding="utf-8") == source

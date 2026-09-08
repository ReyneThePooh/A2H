"""gate_bridge / functional_fixer 离线单元测试（不依赖设备与 LLM 服务）。"""
import json

import pytest

from diff_tester.gate import RepairReport
from pipeline.functional_fixer import FunctionalFixLoop
from pipeline.gate_bridge import (
    build_page_pairs,
    build_unit_page_map,
    find_hap,
    page_for_activity,
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

def _report(trace_id="t1", failure_type="CONTENT_LOSS", suspect_units=None):
    return RepairReport(
        trace_id=trace_id, trace_intent="测试", diverged_step=1,
        failure_type=failure_type, abstract_event={}, expected={}, actual={},
        suspect_units=suspect_units or [],
    )


def test_next_units_union_and_fallback():
    r1 = _report("t1", suspect_units=["Unit_A"])
    r2 = _report("t2", suspect_units=["Unit_B", "Unit_A"])
    assert FunctionalFixLoop._next_units([r1, r2]) == ["Unit_A", "Unit_B"]

    # 任一报告缺 suspect_units → 全量（None）
    assert FunctionalFixLoop._next_units([r1, _report("t3")]) is None
    # 只有 TIMEOUT → 无可增量选择 → 全量
    assert FunctionalFixLoop._next_units([_report("t4", failure_type="TIMEOUT")]) is None


class FakeLLM:
    """返回固定修复代码的假 LLM。"""

    def __init__(self, response: str):
        self.response = response
        self.calls: list[list] = []

    def invoke(self, messages):
        self.calls.append(messages)
        return self.response


def test_fix_file_writes_backup_and_diff(tmp_path):
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

import json

from analyzers.arkts_index import (
    ArkTSIndex,
    build_arkts_index,
    load_arkts_index,
    normalize_route,
    validate_patch_scope,
    validate_static_contract,
)


def _project(tmp_path, *, main_pages=None, main_source=None, detail_source=None):
    root = tmp_path / "harmony"
    main = root / "entry/src/main/ets/pages/Main.ets"
    main.parent.mkdir(parents=True)
    main.write_text(
        main_source
        or """@Entry
@Component
export struct Main {
  build() {
    Column() {
      Text($r('app.string.title')).id('open')
        .onClick(() => { router.pushUrl({ url: 'pages/Detail' }) })
    }
  }
  openDetails() {
    router.pushUrl({ url: 'pages/Unknown' })
  }
}
""",
        encoding="utf-8",
    )
    if detail_source is not None:
        detail = root / "entry/src/main/ets/pages/Detail.ets"
        detail.write_text(detail_source, encoding="utf-8")
    profile = root / "entry/src/main/resources/base/profile/main_pages.json"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text(
        json.dumps({"src": main_pages or ["pages/Main", "pages/Detail"]}),
        encoding="utf-8",
    )
    return root


def test_build_index_extracts_components_methods_and_replay_facts(tmp_path):
    root = _project(
        tmp_path,
        detail_source="""@Entry\n@Component\nstruct Detail {\n  build() { Text('Detail') }\n}\n""",
    )

    index = build_arkts_index(root)
    assert [item.path for item in index.files] == [
        "entry/src/main/ets/pages/Detail.ets",
        "entry/src/main/ets/pages/Main.ets",
    ]
    assert index.registered_routes == ["pages/Main", "pages/Detail"]
    assert index.route_files == {
        "pages/Detail": "entry/src/main/ets/pages/Detail.ets",
        "pages/Main": "entry/src/main/ets/pages/Main.ets",
    }

    main = next(item for item in index.structs if item.name == "Main")
    assert main.entry and main.component
    assert main.build_start_line == 4
    assert main.build_end_line >= main.build_start_line
    assert "open" in main.ids
    assert "title" in main.resource_refs[0]
    assert main.text_literals == []  # resource value is not mistaken for UI text
    assert main.event_handlers == ["onClick"]
    assert main.router_targets == ["pages/Detail", "pages/Unknown"]
    assert all(ref.line > 0 for ref in main.id_refs + main.event_refs + main.router_refs)


def test_index_round_trip_and_deterministic_serialization(tmp_path):
    index = build_arkts_index(_project(tmp_path, detail_source="@Entry struct Detail { build() {} }"))
    target = tmp_path / "out/index.json"
    index.save(target)
    loaded = load_arkts_index(target)
    assert loaded.to_dict() == index.to_dict()
    assert json.dumps(index.to_dict(), ensure_ascii=False, sort_keys=True) == json.dumps(
        loaded.to_dict(), ensure_ascii=False, sort_keys=True
    )
    assert isinstance(ArkTSIndex.from_dict(index.to_dict()), ArkTSIndex)


def test_validate_static_contract_reports_missing_id_and_unregistered_route(tmp_path):
    root = _project(tmp_path, detail_source="@Entry struct Detail { build() {} }")
    index = build_arkts_index(root)
    traces = [
        {
            "trace_id": "t1",
            "initial_state": {"page": "MainActivity"},
            "events": [
                {
                    "step": 1,
                    "target": {"id_hint": "missing_button"},
                    "pre_state": {"page": "MainActivity"},
                    "post_state": {"page": "MainActivity"},
                }
            ],
        }
    ]
    issues = validate_static_contract(
        index,
        {"MainActivity": "pages/Main"},
        traces,
    )
    codes = [item["code"] for item in issues]
    assert codes.count("MISSING_ID") == 1
    assert codes.count("UNREGISTERED_ROUTE") == 1
    missing = next(item for item in issues if item["code"] == "MISSING_ID")
    assert missing["trace_id"] == "t1"
    assert missing["step"] == 1
    assert missing["route"] == "pages/Main"


def test_route_normalization_rejects_escape_and_registry_flags_it(tmp_path):
    assert normalize_route("Ability:pages/Main.ets") == "pages/Main"
    assert normalize_route("pages/Main/") == "pages/Main"
    assert normalize_route("/pages/Main") == ""
    assert normalize_route("https://example.invalid/pages/Main") == ""
    assert normalize_route("C:/pages/Main") == ""
    assert normalize_route("../outside") == ""
    root = _project(tmp_path, main_pages=["pages/Main", "../outside"])
    index = build_arkts_index(root)
    assert any(item["code"] == "INVALID_ROUTE" for item in index.issues)
    assert index.registered_routes == ["pages/Main"]


def test_index_resolves_wrapped_template_root_and_reports_missing_route_file(tmp_path):
    root = tmp_path / "checkout"
    source = root / "template/entry/src/main/ets/pages/Main.ets"
    source.parent.mkdir(parents=True)
    source.write_text("@Entry\n@Component\nstruct Main { build() {} }\n", encoding="utf-8")
    profile = root / "template/entry/src/main/resources/base/profile/main_pages.json"
    profile.parent.mkdir(parents=True)
    profile.write_text(json.dumps({"src": ["pages/Main", "pages/Missing"]}), encoding="utf-8")

    index = build_arkts_index(root)

    assert index.route_files["pages/Main"].endswith("template/entry/src/main/ets/pages/Main.ets")
    assert any(item["code"] == "MISSING_ROUTE_FILE" and item["route"] == "pages/Missing"
               for item in index.issues)


def test_index_ignores_ohos_test_sources_and_reports_ambiguous_routes(tmp_path):
    root = tmp_path / "project"
    main = root / "entry/src/main/ets/pages/Main.ets"
    duplicate = root / "feature/entry/src/main/ets/pages/Main.ets"
    test_source = root / "entry/src/ohosTest/ets/test/Fake.ets"
    unit_source = root / "entry/src/test/ets/test/Unit.ets"
    for path in (main, duplicate, test_source, unit_source):
        path.parent.mkdir(parents=True, exist_ok=True)
    main.write_text("@Entry struct Main { build() {} }\n", encoding="utf-8")
    duplicate.write_text("@Entry struct Duplicate { build() {} }\n", encoding="utf-8")
    test_source.write_text(
        "@Entry struct Fake { build() { router.pushUrl({ url: 'pages/Nope' }) } }\n",
        encoding="utf-8",
    )
    unit_source.write_text(
        "@Entry struct Unit { build() { router.pushUrl({ url: 'pages/Nope' }) } }\n",
        encoding="utf-8",
    )
    profile = root / "entry/src/main/resources/base/profile/main_pages.json"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text(json.dumps({"src": ["pages/Main"]}), encoding="utf-8")

    index = build_arkts_index(root)

    assert all("ohosTest" not in item.path and "/test/" not in item.path
               for item in index.files)
    issue = next(item for item in index.issues if item["code"] == "AMBIGUOUS_ROUTE_FILE")
    assert issue["route"] == "pages/Main"
    assert len(issue["candidates"]) == 2


def test_index_does_not_skip_project_root_named_tests(tmp_path):
    root = tmp_path / "tests" / "harmony"
    page = root / "entry/src/main/ets/pages/Main.ets"
    page.parent.mkdir(parents=True)
    page.write_text("@Entry struct Main { build() {} }\n", encoding="utf-8")

    index = build_arkts_index(root)

    assert [item.path for item in index.files] == [
        "entry/src/main/ets/pages/Main.ets"
    ]


def test_route_registry_scan_ignores_test_profiles(tmp_path):
    root = tmp_path / "source-only"
    source = root / "entry/src/main/ets/pages/Main.ets"
    source.parent.mkdir(parents=True)
    source.write_text("@Entry struct Main { build() {} }\n", encoding="utf-8")
    profile = root / "entry/src/ohosTest/resources/base/profile/main_pages.json"
    profile.parent.mkdir(parents=True)
    profile.write_text(json.dumps({"src": ["pages/Fake"]}), encoding="utf-8")

    index = build_arkts_index(root)

    assert index.route_file == ""
    assert index.registered_routes == []


def test_static_contract_accepts_registered_id_and_route(tmp_path):
    root = _project(
        tmp_path,
        main_source="""@Entry\n@Component\nstruct Main {\n  build() { Button('Open').id('open') }\n}\n""",
        detail_source="@Entry struct Detail { build() { Text('Detail') } }",
    )
    index = build_arkts_index(root)
    traces = [{
        "trace_id": "ok",
        "initial_state": {"page": "MainActivity"},
        "events": [{
            "step": 1,
            "target": {"id_hint": "open"},
            "pre_state": {"page": "MainActivity"},
            "post_state": {"page": "DetailAbility:pages/Detail"},
        }],
    }]
    assert validate_static_contract(index, {"MainActivity": "pages/Main"}, traces) == []


def test_static_contract_does_not_mask_invalid_trace_baseline(tmp_path):
    root = _project(tmp_path)
    index = build_arkts_index(root)
    invalid_v1 = {
        "trace_id": "legacy",
        "schema_version": 1,
        "initial_state": {"page": "MainActivity"},
        "events": [{"step": 1, "target": {"id_hint": "missing"}}],
    }
    invalid_v2 = {
        "trace_id": "incomplete",
        "schema_version": 2,
        "initial_state": {"page": "MainActivity"},
        "events": [{"step": 1, "target": {"id_hint": "missing"},
                     "pre_state": None, "post_state": None}],
    }

    issues = validate_static_contract(
        index, {"MainActivity": "pages/Main"}, [invalid_v1, invalid_v2]
    )

    assert not any(issue.get("code") == "MISSING_ID" for issue in issues)


def test_patch_scope_rejects_changes_outside_indexed_method():
    original = """@Component
struct Main {
  build() { Text('Open').id('open') }
  helper() { return 1 }
}
"""
    inside = original.replace("Text('Open')", "Text('Open now')")
    outside = inside.replace("return 1", "return 2")
    allowed = [{"start_line": 3, "end_line": 3, "reason": "id:open"}]

    assert validate_patch_scope(original, inside, allowed) == []
    issues = validate_patch_scope(original, outside, allowed)
    assert issues and issues[0]["code"] == "PATCH_SCOPE_OUTSIDE_INDEX"


def test_repair_regions_accepts_ability_prefixed_route(tmp_path):
    root = _project(
        tmp_path,
        main_source="""@Entry
@Component
struct Main {
  build() { Text('Open').id('open') }
}
""",
        detail_source="""@Entry
@Component
struct Detail {
  build() { Button('Save').id('save') }
}
""",
    )
    index = build_arkts_index(root)

    regions = index.repair_regions(
        "entry/src/main/ets/pages/Detail.ets", ["save"],
        "EntryAbility:pages/Detail",
    )

    assert regions and regions[0]["reason"] == "id:save"

"""Fault injection for completion, caching, source provenance and continuation."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pipeline.artifacts import ArtifactContractError, load_artifact_manifest
from pipeline.order_determiner import SummaryGenerator, Unit
from pipeline.unit_translator import (
    OutputPlan, SourceReader, TranslationPipeline, TranslationResult, UnitTranslator,
    _parse_json, _safe_format,
)


def translator(tmp_path, responses, planned=True):
    source = tmp_path / "android/app/src/main/java/demo/Main.java"
    source.parent.mkdir(parents=True)
    source.write_text("package demo; class Main { void click() {} }", encoding="utf-8")
    unit = Unit("main", ["java/demo/Main.java"])
    output = OutputPlan("Actual.ets", 0, "planned" if planned else "simple", unit.sources,
                        [{"step": "UI", "read_regions": []}, {"step": "events", "read_regions": []}] if planned else [])
    obj = UnitTranslator.__new__(UnitTranslator)
    obj.llm = SimpleNamespace(invoke=Mock(side_effect=responses))
    obj.translate_agent = SimpleNamespace(run=Mock(return_value="invalid"))
    obj.planner_agent = SimpleNamespace(run=Mock(return_value=json.dumps({"outputs": [output.__dict__]})))
    obj.harmony_root = tmp_path / "harmony"
    obj.tools = SimpleNamespace(resource_mapping={"entries": []})
    obj.reader = SourceReader(tmp_path / "android")
    obj._trans_cache_path = tmp_path / "android/.pipeline_cache/unit_translations.json"
    obj._trans_cache = {}
    return obj, unit, output


def response(code, done=False):
    return json.dumps({"code": code, "done": done})


def test_json_strings_and_source_braces_are_preserved():
    code = 'class X { value = "}"; nested = { one: 1 }; }'
    assert _parse_json(response(code))["code"] == code
    assert _safe_format("{source}", source=code) == code
    assert _parse_json('[{"code":"x"}]') is None
    assert _parse_json('prefix {"code":"x"}') is None


@pytest.mark.parametrize("planner_response, expected", [
    ("", "PLANNER_EMPTY_RESPONSE"),
    ("gateway returned plain text", "PLANNER_INVALID_JSON"),
    (json.dumps({}), "PLANNER_MISSING_OUTPUTS"),
    (json.dumps({"outputs": []}), "PLANNER_EMPTY_OUTPUTS"),
])
def test_planner_failures_preserve_diagnostic_reason(tmp_path, capsys, planner_response, expected):
    obj, unit, _ = translator(tmp_path, [])
    obj.planner_agent.run.return_value = planner_response

    result = obj.translate(unit, "", {})[0]

    assert not result.success
    assert expected in result.error
    assert expected in capsys.readouterr().out


def test_planner_valid_json_with_wrong_top_level_type_is_schema_failure(tmp_path, capsys):
    obj, unit, _ = translator(tmp_path, [])
    obj.planner_agent.run.return_value = json.dumps([{"file": "Actual.ets"}])

    result = obj.translate(unit, "", {})[0]

    assert not result.success
    assert result.status == "planner_failed"
    assert "PLANNER_INVALID_SCHEMA" in result.error
    assert "list" in result.error
    assert "PLANNER_INVALID_SCHEMA" in capsys.readouterr().out


def test_planner_exception_preserves_type_and_reason(tmp_path):
    obj, unit, _ = translator(tmp_path, [])
    obj.planner_agent.run.side_effect = TimeoutError("gateway timed out")

    result = obj.translate(unit, "", {})[0]

    assert not result.success
    assert result.status == "planner_failed"
    assert "PLANNER_EXCEPTION" in result.error
    assert "TimeoutError" in result.error
    assert "gateway timed out" in result.error


def test_planner_non_text_response_is_classified(tmp_path):
    obj, unit, _ = translator(tmp_path, [])
    obj.planner_agent.run.return_value = {"outputs": []}

    result = obj.translate(unit, "", {})[0]

    assert not result.success
    assert "PLANNER_INVALID_RESPONSE" in result.error
    assert "dict" in result.error


def test_planner_invalid_json_is_repaired_once(tmp_path):
    obj, unit, output = translator(tmp_path, [], planned=False)
    obj.planner_agent.run.side_effect = [
        "not json",
        json.dumps({"outputs": [output.__dict__]}),
    ]
    obj.llm.invoke.side_effect = None
    obj.llm.invoke.return_value = response("export class Actual {}")

    result = obj.translate(unit, "", {})[0]

    assert result.success
    assert obj.planner_agent.run.call_count == 2
    assert obj._checkpoint["planner_attempts"][0]["error"].startswith("PLANNER_INVALID_JSON")
    assert obj._checkpoint["planner_attempts"][1]["success"] is True


def test_planner_repair_exhaustion_remains_failure(tmp_path):
    obj, unit, _ = translator(tmp_path, [])
    obj.planner_agent.run.side_effect = ["not json", "still not json"]

    result = obj.translate(unit, "", {})[0]

    assert not result.success
    assert result.status == "planner_failed"
    assert "PLANNER_INVALID_JSON" in result.error
    assert obj.planner_agent.run.call_count == 2
    assert len(obj._checkpoint["planner_attempts"]) == 2


def test_invalid_plan_is_replanned_with_validation_feedback(tmp_path):
    obj, unit, output = translator(tmp_path, [], planned=False)
    invalid = dict(output.__dict__)
    invalid["depends_on"] = ["Missing.ets"]
    obj.planner_agent.run.side_effect = [
        json.dumps({"outputs": [invalid]}),
        json.dumps({"outputs": [output.__dict__]}),
    ]
    obj.llm.invoke.side_effect = None
    obj.llm.invoke.return_value = response("export class Actual {}")

    result = obj.translate(unit, "", {})[0]

    assert result.success
    assert obj.planner_agent.run.call_count == 2
    assert obj.planner_agent.run.call_args_list[1].args[0].find("Unknown output dependencies") >= 0


def test_simple_executor_invalid_json_is_repaired_in_place(tmp_path):
    obj, unit, _ = translator(tmp_path, ["not json"], planned=False)
    obj.translate_agent.run.return_value = response("export class Actual {}")

    result = obj.translate(unit, "", {})[0]

    assert result.success
    assert obj.llm.invoke.call_count == 1
    assert obj.translate_agent.run.call_count == 1
    assert obj._checkpoint["executor_repairs"][0]["file"] == "Actual.ets"


def test_planned_executor_repairs_only_failed_step(tmp_path):
    obj, unit, _ = translator(tmp_path, ["not json", response("export class Actual { run() {} }", True)])
    obj.translate_agent.run.return_value = response("export class Actual {}", False)

    result = obj.translate(unit, "", {})[0]

    assert result.success and result.completed_steps == 2
    assert obj.translate_agent.run.call_count == 1
    assert obj.llm.invoke.call_count == 2
    assert len(obj._checkpoint["executor_repairs"]) == 1
    assert obj._checkpoint["executor_repairs"][0]["file"] == "Actual.ets"
    assert obj._checkpoint["executor_repairs"][0]["step"] == 1


@pytest.mark.parametrize("failure", [TimeoutError("interrupted"), "not json", '{"code": []}'])
def test_partial_planned_output_never_becomes_success_or_cache(tmp_path, failure):
    obj, unit, _ = translator(tmp_path, [response("export class A {}"), failure])
    results = obj.translate(unit, "dependency", {})
    assert results[0].success is False
    assert results[0].completed_steps == 1
    assert obj._trans_cache == {}
    assert not (obj.harmony_root / "entry/src/main/ets/pages/Actual.ets").exists()


def test_final_done_false_is_not_complete(tmp_path):
    obj, unit, _ = translator(tmp_path, [response("export class A {}"), response("export class A { x = 1; }")])
    assert not obj.translate(unit, "", {})[0].success


def test_behavior_view_ids_must_be_exposed_on_generated_controls(tmp_path):
    obj, unit, _ = translator(tmp_path, [response("export class Main {}")], planned=False)
    source = tmp_path / "android/app/src/main/java/demo/Main.java"
    source.write_text(
        "package demo; class Main { void bind() { findViewById(R.id.fore_rain); } }",
        encoding="utf-8",
    )

    result = obj.translate(unit, "", {})[0]

    assert not result.success
    assert result.status == "behavior_contract_failed"
    assert result.error == "BEHAVIOR_CONTRACT_MISSING_IDS: fore_rain"
    assert obj._trans_cache == {}


def test_preserved_behavior_view_ids_allow_translation(tmp_path):
    code = "@Component struct Main { build() { List() {}.id('fore_rain') } }"
    obj, unit, _ = translator(tmp_path, [response(code)], planned=False)
    source = tmp_path / "android/app/src/main/java/demo/Main.java"
    source.write_text(
        "package demo; class Main { void bind() { findViewById(R.id.fore_rain); } }",
        encoding="utf-8",
    )

    result = obj.translate(unit, "", {})[0]

    assert result.success
    assert next(iter(obj._trans_cache.values()))["outputs"][0]["code"] == code


def test_translated_popup_must_expose_active_scope(tmp_path):
    obj, unit, _ = translator(tmp_path, [response("@Component struct Main {}")], planned=False)
    source = tmp_path / "android/app/src/main/java/demo/Main.java"
    source.write_text(
        "package demo; class Main { void show() { new PopupWindow(this); } }",
        encoding="utf-8",
    )

    result = obj.translate(unit, "", {})[0]

    assert not result.success
    assert result.error == "BEHAVIOR_CONTRACT_MISSING_IDS: a2h_active_scope"


def test_translated_popup_with_active_scope_satisfies_contract(tmp_path):
    code = "@Component struct Main { build() { Column() {}.id('a2h_active_scope') } }"
    obj, unit, _ = translator(tmp_path, [response(code)], planned=False)
    source = tmp_path / "android/app/src/main/java/demo/Main.java"
    source.write_text(
        "package demo; class Main { void show() { new PopupWindow(this); } }",
        encoding="utf-8",
    )

    assert obj.translate(unit, "", {})[0].success


def test_step_checkpoint_resumes_with_full_matching_context(tmp_path):
    obj, unit, _ = translator(tmp_path, [response("export class A {}"), TimeoutError("interrupted")])
    assert not obj.translate(unit, "export class Dep {}", {})[0].success
    obj.llm.invoke = Mock(return_value=response("export class A { run(): void {} }", True))
    result = obj.translate(unit, "export class Dep {}", {})[0]
    assert result.success and result.completed_steps == 2
    assert obj.llm.invoke.call_count == 1
    prompt = obj.llm.invoke.call_args.args[0][0]["content"]
    assert "export class Dep {}" in prompt and "export class A {}" in prompt
    entry = next(iter(obj._trans_cache.values()))
    assert entry["status"] == "generated_candidate"
    assert result.status == "generated"


def test_changed_dependency_invalidates_checkpoint_and_cache(tmp_path):
    obj, unit, _ = translator(tmp_path, [response("export class A {}"), TimeoutError("interrupted")])
    obj.translate(unit, "old API", {})
    obj.llm.invoke = Mock(side_effect=[response("export class B {}"), response("export class B { run() {} }", True)])
    assert obj.translate(unit, "new API", {})[0].success
    assert obj.llm.invoke.call_count == 2


def test_cache_fingerprint_covers_resources_prompts_sdk(tmp_path, monkeypatch):
    import pipeline.unit_translator as module
    obj, unit, _ = translator(tmp_path, [])
    baseline = obj._trans_cache_key(unit, "api")
    obj.tools.resource_mapping = {"entries": [{"name": "new"}]}
    assert obj._trans_cache_key(unit, "api") != baseline
    obj.tools.resource_mapping = {"entries": []}
    monkeypatch.setenv("TARGET_SDK_VERSION", "999")
    sdk_key = obj._trans_cache_key(unit, "api")
    assert sdk_key != baseline
    monkeypatch.setattr(module, "SIMPLE_EXECUTOR_PROMPT", module.SIMPLE_EXECUTOR_PROMPT + "new rule")
    assert obj._trans_cache_key(unit, "api") != sdk_key


def test_sdk_component_version_content_invalidates_cache(tmp_path, monkeypatch):
    obj, unit, _ = translator(tmp_path, [])
    sdk = tmp_path / "sdk/arkts"
    sdk.mkdir(parents=True)
    version = sdk / "oh-uni-package.json"
    version.write_text('{"version":"1"}', encoding="utf-8")
    monkeypatch.setenv("HARMONY_SDK_HOME", str(sdk.parent))
    key = obj._trans_cache_key(unit)
    version.write_text('{"version":"2"}', encoding="utf-8")
    assert obj._trans_cache_key(unit) != key


def test_missing_plan_source_and_path_escape_are_rejected(tmp_path):
    obj, unit, output = translator(tmp_path, [])
    output.sources = []
    with pytest.raises(ValueError):
        obj._validate_plan([output], unit)
    output.sources = unit.sources
    output.file = "../outside.ets"
    with pytest.raises(ArtifactContractError):
        obj._validate_plan([output], unit)


def test_completed_external_dependency_is_usable_and_cached(tmp_path):
    obj, unit, output = translator(tmp_path, [response("export class Db {}")], planned=False)
    output.depends_on = ["mycity.ets"]
    obj.planner_agent.run.return_value = json.dumps({"outputs": [output.__dict__]})
    dependencies = {"MyCity.ets": "export class MyCity {}"}
    assert obj.translate(unit, "MyCity API", dependencies.copy())[0].success
    assert "export class MyCity {}" in obj.llm.invoke.call_args.args[0][0]["content"]
    assert obj.translate(unit, "MyCity API", dependencies.copy())[0].success
    assert obj.planner_agent.run.call_count == 1
    assert obj.llm.invoke.call_count == 1


def test_outputs_are_executed_in_dependency_order(tmp_path):
    obj, unit, output = translator(tmp_path, [], planned=False)
    helper = OutputPlan("Helper.ets", 9, "simple", unit.sources)
    output.depends_on = ["Helper.ets"]
    obj.planner_agent.run.return_value = json.dumps({"outputs": [output.__dict__, helper.__dict__]})
    obj.llm.invoke.side_effect = [response("export class Helper {}"), response("export class Main {}")]
    results = obj.translate(unit, "", {})
    assert [item.file_name for item in results] == ["Helper.ets", "Actual.ets"]
    assert all(item.success for item in results)
    assert "export class Helper {}" in obj.llm.invoke.call_args.args[0][0]["content"]


@pytest.mark.parametrize("dependency, expected", [
    ("Actual.ets", "Output depends on itself: Actual.ets"),
    ("Missing.ets", "Unknown output dependencies for Actual.ets: ['Missing.ets']"),
])
def test_invalid_output_dependencies_remain_explicit_failures(tmp_path, dependency, expected):
    obj, unit, output = translator(tmp_path, [], planned=False)
    output.depends_on = [dependency]
    obj.planner_agent.run.return_value = json.dumps({"outputs": [output.__dict__]})
    result = obj.translate(unit, "", {})[0]
    assert not result.success and result.status == "plan_validation_failed" and expected in result.error
    obj.llm.invoke.assert_not_called()
    assert not obj._trans_cache


def test_cyclic_outputs_and_overwriting_dependencies_are_rejected(tmp_path):
    obj, unit, output = translator(tmp_path, [], planned=False)
    other = OutputPlan("Other.ets", 1, "simple", unit.sources, depends_on=[output.file])
    output.depends_on = [other.file]
    with pytest.raises(ValueError, match="Cyclic output dependencies"):
        obj._validate_plan([output, other], unit)
    with pytest.raises(ValueError, match="overwrites completed dependency"):
        obj._validate_plan([output], unit, {output.file: "verified dependency"})


def test_source_reader_resolves_exact_paths_and_rejects_ambiguity(tmp_path):
    for package in ("one", "two"):
        path = tmp_path / f"app/src/main/java/{package}/Main.java"
        path.parent.mkdir(parents=True)
        path.write_text(package, encoding="utf-8")
    reader = SourceReader(tmp_path)
    assert "two" in reader.read_all(["java/two/Main.java"])
    with pytest.raises(ArtifactContractError):
        reader.read_all(["Main.java"])
    with pytest.raises(FileNotFoundError):
        reader.read_all(["Missing.java"])


def test_unit_dependency_failure_blocks_downstream(tmp_path):
    obj = TranslationPipeline.__new__(TranslationPipeline)
    obj.project_root = str(tmp_path)
    obj.summaries = {}
    obj.translator = SimpleNamespace(harmony_root=tmp_path / "harmony",
        translate=Mock(return_value=[TranslationResult(unit_name="base", error="failed")]),
        _api_summary=lambda code: code)
    result = obj.run([[Unit("base", ["Base.java"])], [Unit("screen", ["Screen.java"])]], {"screen": {"base"}})
    assert obj.translator.translate.call_count == 1
    assert result[1].status == "blocked_dependency"
    assert load_artifact_manifest(obj.translator.harmony_root)["status"] == "incomplete"


@pytest.mark.parametrize("group_success", [True, False])
def test_cycle_is_translated_together_and_downstream_waits_for_complete_group(tmp_path, group_success):
    obj = TranslationPipeline.__new__(TranslationPipeline)
    obj.project_root, obj.summaries = str(tmp_path), {}
    calls = []
    def translate(unit, context, available):
        calls.append((unit, context, available.copy()))
        if "A.java" in unit.sources:
            assert set(unit.sources) == {"A.java", "B.java"}
            assert available == {"Base.ets": "export class Base {}"}
        return [TranslationResult(unit_name=unit.name, file_name=source.replace(".java", ".ets"),
                    sources=[source], code=f"export class {Path(source).stem} {{}}",
                    success=group_success if "A.java" in unit.sources else True)
                for source in unit.sources]
    obj.translator = SimpleNamespace(harmony_root=tmp_path / "harmony", translate=translate,
                                     _api_summary=lambda code: code)
    units = {name: Unit(name, [name + ".java"]) for name in ("Base", "A", "B", "Consumer", "Independent")}
    deps = {"A": {"B", "Base"}, "B": {"A"}, "Consumer": {"B"}}
    # Same-layer cycles in a saved plan must work even if input layers/order are stale.
    result = obj.run([list(reversed(list(units.values())))], deps)
    assert deps == {"A": {"B", "Base"}, "B": {"A"}, "Consumer": {"B"}}
    assert any(call[0].name == "Independent" for call in calls)
    manifest = load_artifact_manifest(obj.translator.harmony_root)
    assert sorted(source for unit in manifest["units"] for source in unit["sources"]) == sorted(n + ".java" for n in units)
    if group_success:
        consumer = next(call for call in calls if call[0].name == "Consumer")
        assert set(consumer[2]) == {"A.ets", "B.ets"}
        assert all(item.success for item in result)
        assert manifest["status"] == "generated"
    else:
        assert not any(call[0].name == "Consumer" for call in calls)
        assert next(item for item in result if item.unit_name == "Consumer").status == "blocked_dependency"
        assert manifest["status"] == "incomplete"


def test_summary_cache_tracks_added_changed_removed_files(tmp_path, monkeypatch):
    generator = SummaryGenerator(None)
    calls = []
    monkeypatch.setattr(generator, "_summarize_java", lambda path, summary: calls.append(path))
    cache = str(tmp_path / "cache/summaries.json")
    original = {"A.java": "class A { int a; }", "B.java": "class B {}"}
    generator.generate_all(str(tmp_path), original, cache)
    assert set(calls) == {"A.java", "B.java"}
    calls.clear()
    generator.generate_all(str(tmp_path), original, cache)
    assert calls == []
    changed = {"A.java": "class A { int b; }", "C.java": "class C {}"}
    summaries = generator.generate_all(str(tmp_path), changed, cache)
    assert set(calls) == {"A.java", "C.java"}
    assert set(summaries) == {"A.java", "C.java"}

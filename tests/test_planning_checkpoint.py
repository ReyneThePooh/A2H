"""Planning resume recovers exact typed state without querying any model."""
from dataclasses import asdict
import json

import pytest

from analyzers.static import (FileSummary, XmlSummary, MethodSummary,
                              ParamSummary, ReturnSummary, FieldSummary)
from pipeline.order_determiner import Unit
from pipeline.planning_checkpoint import load_planning, save_planning
from run_control import ResumeMismatch, fingerprint


@pytest.fixture
def plan():
    java = FileSummary("java/demo/Main.java", class_name="Main", package="demo",
                       class_purpose="首页与持久化状态", is_stateful=True,
                       methods=[MethodSummary("save", "save(String city)", 10, 20, 11, "public",
                           purpose="保存城市", params=[ParamSummary("city", "String", "城市名称")],
                           returns=ReturnSummary("boolean", "保存成功"), side_effects=["preferences"])],
                       fields=[FieldSummary("city", "String", "private", 3, "当前城市")],
                       resource_refs={"layout": ["main"]}, type_references=["Store"],
                       intent_targets=["Details"], has_lifecycle=True)
    xml = XmlSummary("res/layout/main.xml", xml_type="layout", root_widget="LinearLayout",
                     widgets=["TextView", "Button"], ids=["save"],
                     resource_refs={"string": ["city"]}, event_handling=["save city"])
    helper = FileSummary("java/demo/Store.java", class_name="Store", class_type="util",
                         methods=[MethodSummary("load", "load()", 2, 4, 3, "public")])
    return ([[Unit("store", [helper.file_path], "存储")],
             [Unit("screen", [java.file_path, xml.file_path], "主页面")]],
            {"screen": {"store"}, "store": set()},
            {java.file_path: java, xml.file_path: xml, helper.file_path: helper})


def test_exact_nested_dataclasses_roundtrip_without_model_calls(tmp_path, plan, monkeypatch):
    import pipeline.agents
    import pipeline.order_determiner
    def prohibited(*args, **kwargs):
        raise AssertionError("checkpoint restore must not invoke planning or a model")
    monkeypatch.setattr(pipeline.agents, "create_pipeline_llm", prohibited)
    monkeypatch.setattr(pipeline.order_determiner.OrderDeterminer, "run", prohibited)
    target = tmp_path / "planning.json"
    save_planning(target, *plan)
    layers, deps, summaries = load_planning(target)
    assert layers == plan[0] and deps == plan[1] and summaries == plan[2]
    assert isinstance(layers[1][0], Unit)
    summary = summaries["java/demo/Main.java"]
    assert isinstance(summary.methods[0], MethodSummary)
    assert isinstance(summary.methods[0].params[0], ParamSummary)
    assert isinstance(summary.methods[0].returns, ReturnSummary)
    assert isinstance(summary.fields[0], FieldSummary)
    assert isinstance(summaries["res/layout/main.xml"], XmlSummary)
    assert summaries["java/demo/Store.java"].methods[0].returns is None


def test_checkpoint_serialization_is_stable_across_dictionary_set_order(tmp_path, plan):
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    save_planning(first, *plan)
    layers, deps, summaries = plan
    save_planning(second, layers, dict(reversed(list(deps.items()))),
                  dict(reversed(list(summaries.items()))))
    assert first.read_bytes() == second.read_bytes()


@pytest.mark.parametrize("damage", ["truncated", "unknown_version", "changed_payload", "missing_hash"])
def test_damaged_checkpoint_raises_resume_mismatch_instead_of_replanning(tmp_path, plan, damage):
    target = tmp_path / "planning.json"
    save_planning(target, *plan)
    data = json.loads(target.read_text(encoding="utf-8"))
    if damage == "truncated":
        target.write_text('{"payload":', encoding="utf-8")
    else:
        if damage == "unknown_version":
            data["schema_version"] = 999
        elif damage == "changed_payload":
            data["payload"]["layers"][0][0]["name"] = "corrupted"
        else:
            data.pop("content_sha256")
        target.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ResumeMismatch):
        load_planning(target)


@pytest.mark.parametrize("damage", ["nested_type", "summary_path", "duplicate_unit", "unknown_dependency", "missing_summary"])
def test_valid_hash_cannot_bypass_schema_validation(tmp_path, plan, damage):
    target = tmp_path / "planning.json"
    save_planning(target, *plan)
    data = json.loads(target.read_text(encoding="utf-8"))
    payload = data["payload"]
    if damage == "nested_type":
        payload["summaries"]["java/demo/Main.java"]["data"]["methods"][0]["params"] = "invalid"
    elif damage == "summary_path":
        payload["summaries"]["java/demo/Main.java"]["data"]["file_path"] = "Other.java"
    elif damage == "duplicate_unit":
        payload["layers"][1][0]["name"] = "store"
    elif damage == "unknown_dependency":
        payload["deps"]["screen"] = ["missing"]
    else:
        del payload["summaries"]["java/demo/Main.java"]
    data["content_sha256"] = fingerprint(payload)
    target.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ResumeMismatch):
        load_planning(target)


def test_missing_checkpoint_is_explicit_resume_failure(tmp_path):
    with pytest.raises(ResumeMismatch, match="missing or unreadable"):
        load_planning(tmp_path / "missing.json")


def test_invalid_replacement_preserves_previous_valid_checkpoint(tmp_path, plan):
    target = tmp_path / "planning.json"
    save_planning(target, *plan)
    before = target.read_bytes()
    with pytest.raises(ResumeMismatch):
        save_planning(target, [[Unit("unknown", ["absent.java"])]], {"unknown": set()}, plan[2])
    assert target.read_bytes() == before
    assert load_planning(target) == plan

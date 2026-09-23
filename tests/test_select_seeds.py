"""Offline tests for the short-seed selection CLI policy."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


_MODULE_PATH = Path(__file__).parents[1] / "tools" / "select_seeds.py"
_SPEC = spec_from_file_location("select_seeds", _MODULE_PATH)
assert _SPEC and _SPEC.loader
select_seeds = module_from_spec(_SPEC)
_SPEC.loader.exec_module(select_seeds)


def _candidate(tmp_path, name, pages, steps):
    path = tmp_path / name
    return {"fp": path, "trace": {}, "n": steps, "pages": list(pages)}


def test_baseline_errors_rejects_legacy_seed_without_mutating_it():
    legacy = {
        "trace_id": "legacy",
        "app_pkg_android": "com.example.android",
        "app_pkg_harmony": "com.example.harmony",
        "events": [{"step": 1, "action": "BACK", "post_state": {
            "page": "MainActivity", "texts": {}, "widgets": {},
            "values": {}, "list_counts": {},
        }}],
    }

    errors = select_seeds.baseline_errors(legacy)

    assert errors
    assert any("Trace v2 required" in error for error in errors)
    assert "schema_version" not in legacy


def test_select_candidates_prefers_shorter_equal_gain(tmp_path):
    long_main = _candidate(tmp_path, "long.json", ["MainActivity"], 4)
    short_main = _candidate(tmp_path, "short.json", ["MainActivity"], 2)
    profile = _candidate(tmp_path, "profile.json", ["ProfileActivity"], 3)

    chosen = select_seeds.select_candidates(
        [long_main, short_main, profile], max_count=2)

    assert [candidate["fp"].name for candidate in chosen] == [
        "short.json", "profile.json"
    ]


def test_select_candidates_fills_slots_with_shortest_creation_flow(tmp_path):
    ordinary = _candidate(tmp_path, "ordinary.json", ["MainActivity"], 2)
    creation_long = _candidate(tmp_path, "creation-long.json", ["NewBoardActivity"], 6)
    creation_short = _candidate(tmp_path, "creation-short.json", ["NewBoardActivity"], 3)

    chosen = select_seeds.select_candidates(
        [ordinary, creation_long, creation_short], max_count=3)

    assert [candidate["fp"].name for candidate in chosen] == [
        "ordinary.json", "creation-short.json", "creation-long.json"
    ]

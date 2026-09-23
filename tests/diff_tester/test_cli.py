"""CLI exit status regressions for machine-driven differential replay."""
from types import SimpleNamespace

import pytest

from diff_tester import cli
from diff_tester.adapters import harmony as harmony_module
from diff_tester import replayer as replayer_module
from diff_tester.oracle import PagePairs


class _Harmony:
    def __init__(self, cfg, bundle, serial):
        self.cfg = cfg
        self.bundle = bundle
        self.serial = serial

    def install(self, hap):
        self.hap = hap

    def ensure_ready(self):
        return None


def _args(tmp_path):
    return SimpleNamespace(
        config=None,
        bundle="com.example.hm",
        device="fake-device",
        hap=None,
        page_pairs=None,
        traces=str(tmp_path / "traces"),
        out=str(tmp_path / "results"),
    )


@pytest.mark.parametrize(("passed", "status", "exit_code"), [
    (True, "PASS", 0),
    (False, "FAIL", 1),
    (False, "INCONCLUSIVE", 1),
    (True, "INCONCLUSIVE", 1),
])
def test_replay_exit_code_reflects_decisive_trace_outcomes(
        tmp_path, monkeypatch, passed, status, exit_code):
    cfg = SimpleNamespace(
        device=SimpleNamespace(hdc_path="fake-hdc"),
        oracle=SimpleNamespace(page_stem_sim_min=0.5),
    )
    trace = SimpleNamespace(trace_id="trace")
    result = SimpleNamespace(
        passed=passed,
        status=status,
        to_dict=lambda: {"trace_id": "trace", "passed": passed, "status": status},
    )
    monkeypatch.setattr(cli.Config, "load", lambda _path: cfg)
    monkeypatch.setattr(cli, "_check_tool", lambda *_args: None)
    monkeypatch.setattr(cli, "_load_traces", lambda _path: [trace])
    monkeypatch.setattr(harmony_module, "HarmonyAdapter", _Harmony)
    monkeypatch.setattr(PagePairs, "load", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(replayer_module, "replay", lambda *_args, **_kwargs: result)

    assert cli.cmd_replay(_args(tmp_path)) == exit_code


def test_replay_empty_trace_set_is_not_success(tmp_path, monkeypatch):
    cfg = SimpleNamespace(
        device=SimpleNamespace(hdc_path="fake-hdc"),
        oracle=SimpleNamespace(page_stem_sim_min=0.5),
    )
    monkeypatch.setattr(cli.Config, "load", lambda _path: cfg)
    monkeypatch.setattr(cli, "_check_tool", lambda *_args: None)
    monkeypatch.setattr(cli, "_load_traces", lambda _path: [])
    monkeypatch.setattr(harmony_module, "HarmonyAdapter", _Harmony)
    monkeypatch.setattr(PagePairs, "load", lambda *_args, **_kwargs: object())

    assert cli.cmd_replay(_args(tmp_path)) == 1

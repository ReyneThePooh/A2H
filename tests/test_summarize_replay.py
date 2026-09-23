"""Offline diagnostics for persisted differential replay evidence."""
import json

from tools.summarize_replay import main, summarize_paths


def _result(*, trace_id="seed", kind="L2_CONTENT", cause="translation", step=2):
    return {
        "trace_id": trace_id,
        "status": "FAIL",
        "stop_reason": kind,
        "divergence": {
            "trace_id": trace_id,
            "diverged_step": step,
            "kind": kind,
            "cause_class": cause,
            "detail": {"failed_predicates": ["l2.texts", "l2.values"]},
        },
    }


def test_empty_workspace_reports_no_evidence(tmp_path, capsys):
    summary = summarize_paths([tmp_path])

    assert summary["evidence"] is False
    assert summary["record_count"] == 0
    assert main([str(tmp_path)]) == 0
    assert "无证据" in capsys.readouterr().out


def test_result_file_aggregates_required_diagnostics(tmp_path):
    path = tmp_path / "result.json"
    path.write_text(json.dumps(_result()), encoding="utf-8")

    summary = summarize_paths([path])

    assert summary["evidence"] is True
    assert summary["divergence_kinds"] == {"L2_CONTENT": 1}
    assert summary["failed_predicates"] == {"l2.texts": 1, "l2.values": 1}
    assert summary["cause_classes"] == {"translation": 1}
    assert summary["diverged_steps"] == {"2": 1}
    assert summary["baseline_invalid"] == 0


def test_result_file_expands_all_persisted_divergences(tmp_path):
    payload = _result()
    payload["divergences"] = [
        payload["divergence"],
        {
            "trace_id": "seed",
            "diverged_step": 4,
            "kind": "L2_CONTENT",
            "cause_class": "translation",
            "detail": {"failed_predicates": ["l2.values"]},
        },
    ]
    path = tmp_path / "result.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    summary = summarize_paths([path])

    assert summary["record_count"] == 2
    assert summary["divergence_kinds"] == {"L2_CONTENT": 2}
    assert summary["diverged_steps"] == {"2": 1, "4": 1}
    assert summary["failed_predicates"] == {"l2.texts": 1, "l2.values": 2}


def test_runs_and_history_copies_are_counted_once_per_round(tmp_path):
    runs = tmp_path / "runs" / "round_001" / "seed"
    history = tmp_path / "history"
    runs.mkdir(parents=True)
    history.mkdir()
    result = _result()
    (runs / "result.json").write_text(json.dumps(result), encoding="utf-8")
    report = {
        "trace_id": "seed",
        "diverged_step": 2,
        "failure_type": "L2_CONTENT",
        "cause_class": "translation",
        "evidence": {"detail": {"failed_predicates": ["l2.texts", "l2.values"]}},
    }
    (history / "round_001.json").write_text(
        json.dumps({"reports": [report]}), encoding="utf-8"
    )

    summary = summarize_paths([tmp_path])

    assert summary["record_count"] == 1
    assert summary["divergence_kinds"] == {"L2_CONTENT": 1}


def test_history_baseline_invalid_is_counted(tmp_path):
    path = tmp_path / "history" / "round_002.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "reports": [
                    {
                        "trace_id": "bad-seed",
                        "diverged_step": 0,
                        "failure_type": "BASELINE_INVALID",
                        "cause_class": "baseline",
                        "evidence": {"detail": {"failed_predicates": []}},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    summary = summarize_paths([path])

    assert summary["baseline_invalid"] == 1
    assert summary["divergence_kinds"] == {"BASELINE_INVALID": 1}
    assert summary["cause_classes"] == {"baseline": 1}

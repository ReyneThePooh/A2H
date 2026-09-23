"""报告输出应保留继续回放期间观察到的全部分叉。"""

from __future__ import annotations

import json

from diff_tester.report import _render_md, write_reports
from diff_tester.schemas import DivergenceReport, StateVector, TraceResult


def _state(page: str, text: str) -> StateVector:
    return StateVector(page, {text: 1}, {}, {}, {})


def _divergence(step: int, *, soft: bool, text: str) -> DivergenceReport:
    return DivergenceReport(
        trace_id="trace-multi",
        diverged_step=step,
        kind="L2_CONTENT",
        detail={"missing_texts": [text], "jaccard": 0.5},
        android_state=_state("pages/Main", text),
        harmony_state=_state("pages/Main", "actual"),
        artifacts_dir=f"artifacts/step-{step}",
        category="TRANSLATION",
        phase="compare",
        cause_class="translation",
        action_executed=True,
        soft_failure=soft,
    )


def _result_with_divergences() -> TraceResult:
    first = _divergence(1, soft=True, text="first missing")
    second = _divergence(2, soft=True, text="second missing")
    return TraceResult(
        trace_id="trace-multi",
        total_steps=3,
        executed_steps=3,
        passed=False,
        first_divergence_step=1,
        divergence=first,
        divergences=[first, second],
        status="FAIL",
        verified_steps=1,
        soft_failed_steps=[1, 2],
    )


def test_render_md_includes_each_continued_content_divergence():
    result = _result_with_divergences()

    report = _render_md(
        {"traces": 1, "soft_failed_steps": 2, "divergence_kinds": {"L2_CONTENT": 2}},
        [result],
    )

    assert "trace-multi · step 1 · 状态分叉·语义内容" in report
    assert "trace-multi · step 2 · 状态分叉·语义内容" in report
    assert "first missing" in report and "second missing" in report
    assert report.count("回放控制：软失败，已继续后续步骤") == 2
    assert "artifacts/step-1" in report and "artifacts/step-2" in report


def test_write_reports_keeps_divergences_in_json_and_legacy_fallback(tmp_path):
    result = _result_with_divergences()
    json_path, md_path = write_reports({}, [result], str(tmp_path))

    saved = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert len(saved["results"][0]["divergences"]) == 2
    assert saved["results"][0]["divergences"][1]["diverged_step"] == 2
    assert json_path.endswith("report.json") and md_path.endswith("report.md")

    # A result created by an old caller can have only the singular field.
    legacy = _result_with_divergences()
    legacy.divergences = []
    legacy_report = _render_md({}, [legacy])
    assert "trace-multi · step 1 · 状态分叉·语义内容" in legacy_report
    assert "trace-multi · step 2 · 状态分叉·语义内容" not in legacy_report

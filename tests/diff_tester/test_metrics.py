"""M5 指标公式单测（设计文档 §4.9）+ 归因决策纯函数单测。"""
import pytest

from diff_tester.attribution import decide_category
from diff_tester.metrics import compute_metrics
from diff_tester.schemas import DivergenceReport, StepRecord, TraceResult


def _external_evidence(*, recovered=True, accepted=True, attempted=True):
    evidence = {
        "id": "external_protocol",
        "schema_version": 1,
        "declared_surface": "photo_picker",
        "actual_surface": "photo_picker" if accepted else None,
        "ownership": "system",
        "policy": "dismiss_and_compare",
        "accepted": accepted,
        "recovery_attempted": attempted,
        "recovered": recovered,
        "recovery_action": "BACK",
    }
    if attempted and not recovered:
        evidence["recovery_error"] = "surface remained"
    return evidence


def _steps(n_pass, fail_kind=None, match_kind="MATCHED", pages=None):
    pages = pages or ["MainActivity"] * (n_pass + (1 if fail_kind else 0))
    out = [StepRecord(step=i + 1, action="CLICK", expected_page=pages[i],
                      match_kind=match_kind, match_score=0.9, passed=True,
                      verified=True, action_executed=True, status="PASS")
           for i in range(n_pass)]
    if fail_kind:
        out.append(StepRecord(step=n_pass + 1, action="CLICK",
                              expected_page=pages[n_pass],
                              match_kind="UNMAPPED" if fail_kind == "EXEC_UNMAPPED" else match_kind,
                              match_score=0.3 if fail_kind == "EXEC_UNMAPPED" else 0.9,
                              verdict_kind=fail_kind, passed=False,
                              verified=not fail_kind.startswith("EXEC_"),
                              action_executed=not fail_kind.startswith("EXEC_"), status="FAIL"))
    return out


def _fixture_results():
    # t1：10 步全通过
    t1 = TraceResult(
        trace_id="t1", total_steps=10, executed_steps=10, passed=True,
        steps=_steps(10), android_pages=["MainActivity"],
    )
    # t2：第 6 步状态分叉（L1_PAGE，已执行），归因 TRANSLATION/navigation
    t2 = TraceResult(
        trace_id="t2", total_steps=10, executed_steps=6, passed=False,
        first_divergence_step=6,
        steps=_steps(5, fail_kind="L1_PAGE",
                     pages=["MainActivity"] * 5 + ["DetailActivity"]),
        android_pages=["MainActivity", "DetailActivity"],
        divergence=DivergenceReport(
            trace_id="t2", diverged_step=6, kind="L1_PAGE",
            detail={"defect_type": "navigation"},
            android_state=None, harmony_state=None,
            confirmed=True, category="TRANSLATION",
        ),
    )
    # t3：第 1 步执行分叉（EXEC_UNMAPPED，未执行）
    t3 = TraceResult(
        trace_id="t3", total_steps=5, executed_steps=0, passed=False,
        first_divergence_step=1,
        steps=_steps(0, fail_kind="EXEC_UNMAPPED", pages=["MainActivity"]),
        android_pages=["MainActivity"],
        divergence=DivergenceReport(
            trace_id="t3", diverged_step=1, kind="EXEC_UNMAPPED",
            detail={"defect_type": "state"},
            android_state=None, harmony_state=None,
        ),
    )
    return [t1, t2, t3]


def test_metrics_formulas():
    m = compute_metrics(_fixture_results())
    assert m["traces"] == 3
    assert m["total_events"] == 25
    # Execution includes the action whose post-state diverged.
    assert m["R_replay"] == pytest.approx(16 / 25)
    # R_eq = 通过步数 / 已执行步数 = (10 + 5 + 0) / (10 + 6 + 0)
    assert m["R_eq"] == pytest.approx(15 / 16, abs=1e-4)
    assert m["trace_pass_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert m["avg_norm_divergence_depth"] == pytest.approx((1 + 0.5 + 0) / 3, abs=1e-4)
    # 页面覆盖对齐率：{Main, Detail} 中通过步覆盖 {Main} → 0.5
    assert m["page_coverage_align_rate"] == pytest.approx(0.5)
    # 控件召回率：17 个带 target 的步中 1 个 UNMAPPED
    assert m["widget_recall"] == pytest.approx(16 / 17, abs=1e-4)
    assert m["unmapped_count"] == 1
    # 缺陷谱
    assert m["divergence_kinds"] == {"L1_PAGE": 1, "EXEC_UNMAPPED": 1}
    assert m["defect_categories"] == {"TRANSLATION": 1}
    assert m["defect_spectrum"] == {"navigation": 1, "state": 1}


def test_metrics_all_pass():
    r = TraceResult(trace_id="t", total_steps=4, executed_steps=4, passed=True,
                    steps=_steps(4), android_pages=["MainActivity"])
    m = compute_metrics([r])
    assert m["R_replay"] == 1.0
    assert m["R_eq"] == 1.0
    assert m["trace_pass_rate"] == 1.0
    assert m["page_coverage_align_rate"] == 1.0


def test_metrics_empty():
    metrics = compute_metrics([])
    assert metrics["traces"] == 0
    assert metrics["R_eq"] is None and metrics["R_replay"] is None
    assert metrics["R_eq_direct"] is None and metrics["R_eq_policy"] is None
    assert metrics["direct_verified_steps"] == metrics["mediated_steps"] == 0
    assert metrics["external_recovery_failures"] == 0


def test_metrics_separate_direct_mediated_and_failed_recovery():
    direct = StepRecord(step=1, action="CLICK", passed=True, verified=True,
                        action_executed=True, status="PASS")
    mediated = StepRecord(
        step=2, action="CLICK", passed=True, verified=True,
        action_executed=True, status="PASS",
        external_surface=_external_evidence(),
    )
    recovery_failed = StepRecord(
        step=3, action="CLICK", passed=False, verified=False,
        action_executed=True, status="FAIL",
        external_surface=_external_evidence(recovered=False),
    )
    result = TraceResult("policy", 3, 3, False,
                         steps=[direct, mediated, recovery_failed])

    metrics = compute_metrics([result])

    assert metrics["compared_steps"] == 2
    assert metrics["policy_evaluated_steps"] == 3
    assert metrics["direct_verified_steps"] == 1
    assert metrics["mediated_steps"] == 1
    assert metrics["external_recovery_failures"] == 1
    assert metrics["R_eq_direct"] == pytest.approx(1 / 3, abs=1e-4)
    assert metrics["R_eq_policy"] == pytest.approx(2 / 3, abs=1e-4)
    assert metrics["R_eq"] == metrics["R_eq_policy"]
    assert metrics["per_trace"][0]["mediated_steps"] == 1
    assert metrics["per_trace"][0]["external_recovery_failures"] == 1


def test_metrics_reject_legacy_recovered_surface_as_success():
    step = StepRecord(
        step=1, action="CLICK", passed=True, verified=True,
        action_executed=True, status="PASS",
        external_surface={"id": "external_surface", "recovered": True},
    )

    serialized = TraceResult("legacy", 1, 1, True, status="PASS",
                             verified_steps=1, steps=[step]).to_dict()
    restored = TraceResult.from_dict(serialized)
    metrics = compute_metrics([restored])

    assert metrics["direct_verified_steps"] == 0
    assert metrics["mediated_steps"] == 0
    assert metrics["R_eq_direct"] == 0.0
    assert metrics["R_eq_policy"] == metrics["R_eq"] == 0.0


def test_protocol_rejection_is_evaluated_without_claiming_l0_l2_comparison():
    step = StepRecord(
        step=1, action="CLICK", passed=False, verified=False,
        action_executed=True, status="FAIL",
        external_surface=_external_evidence(
            accepted=False, attempted=False, recovered=False,
        ),
    )

    metrics = compute_metrics([TraceResult("missing", 1, 1, False, steps=[step])])

    assert metrics["compared_steps"] == 0
    assert metrics["policy_evaluated_steps"] == 1
    assert metrics["R_eq_direct"] == metrics["R_eq_policy"] == 0.0


# ---------------------------------------------------------------------------
# 归因四分类决策（§6.6 纯函数）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("reproduced,runs,wl,android_fails,expect_cat,expect_conf", [
    (2, 3, False, False, "NOISE", False),          # 复现不足 → 噪声
    (3, 3, True, False, "PLATFORM", True),         # 白名单命中 → 平台差异
    (3, 3, False, True, "SOURCE_BUG", True),       # 安卓端也异常 → 源应用缺陷
    (3, 3, False, False, "TRANSLATION", True),     # 其余 → 翻译缺陷
    (3, 3, False, None, "TRANSLATION", True),      # 无安卓设备 → 保守归翻译
])
def test_decide_category(reproduced, runs, wl, android_fails, expect_cat, expect_conf):
    cat, conf = decide_category(reproduced, runs, wl, android_fails)
    assert cat == expect_cat and conf == expect_conf


# ---------------------------------------------------------------------------
# 序列化往返
# ---------------------------------------------------------------------------

def test_trace_result_roundtrip():
    import json
    results = _fixture_results()
    for r in results:
        d = json.loads(json.dumps(r.to_dict()))
        r2 = TraceResult.from_dict(d)
        assert r2.trace_id == r.trace_id
        assert r2.total_steps == r.total_steps
        assert r2.passed == r.passed
        assert len(r2.steps) == len(r.steps)
        if r.divergence:
            assert r2.divergence.kind == r.divergence.kind
            assert r2.divergence.detail == r.divergence.detail

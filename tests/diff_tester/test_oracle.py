"""M2 分层预言单测：构造等价/不等价状态对，验证 L0–L2 判定（§6.3）。"""
import pytest

from diff_tester.config import OracleConfig
from diff_tester.oracle import (PagePairs, compare, compare_keyed,
                                multiset_jaccard, widget_align_rate)
from diff_tester.schemas import StateVector

CFG = OracleConfig()


def mk_state(page="MainActivity", texts=None, widgets=None, values=None,
             list_counts=None, alive=True, crash_sig=None):
    return StateVector(
        page=page,
        texts=texts if texts is not None else {"欢迎": 1, "登录": 1, "设置": 1},
        widgets=widgets if widgets is not None else {"button": ["登录", "设置"]},
        values=values if values is not None else {},
        list_counts=list_counts if list_counts is not None else {},
        alive=alive, crash_sig=crash_sig,
    )


PAIRS = PagePairs()


# ---------------------------------------------------------------------------
# 等价
# ---------------------------------------------------------------------------

def test_equivalent_states_pass():
    exp = mk_state(page="MainActivity")
    act = mk_state(page="MainAbility")     # 启发式词干 main == main
    v = compare(exp, act, PAIRS, CFG)
    assert v.passed and v.kind is None


def test_persisted_dynamic_timestamps_are_remasked_before_comparison():
    exp = mk_state(texts={"欢迎": 1, "上次更新时间：40 PM": 1})
    act = mk_state(texts={"欢迎": 1, "上次更新时间：53 AM": 1})
    v = compare(exp, act, PAIRS, CFG)
    assert v.passed and v.detail["jaccard"] == 1.0


def test_explicit_empty_masks_preserve_timestamp_differences():
    exp = mk_state(texts={"上次更新时间：40 PM": 1})
    act = mk_state(texts={"上次更新时间：53 AM": 1})
    assert not compare(exp, act, PAIRS, CFG, masks=[]).passed


@pytest.mark.parametrize("label", ["", "12:34", "<VOLATILE>"])
def test_empty_or_volatile_label_does_not_hide_missing_control(label):
    exp = mk_state(widgets={"button": [label]})
    act = mk_state(widgets={})
    verdict = compare(exp, act, PAIRS, CFG)
    assert not verdict.passed and verdict.detail["widget_align_rate"] == 0


def test_unlabelled_controls_still_compare_by_count():
    exp = mk_state(widgets={"button": ["", ""]})
    act = mk_state(widgets={"button": [""]})
    verdict = compare(exp, act, PAIRS, CFG)
    assert not verdict.passed and verdict.detail["widget_align_rate"] == 0.5


@pytest.mark.parametrize("actual", [{}, {"updated": ""}])
def test_volatile_value_is_distinct_from_missing_or_empty(actual):
    exp = mk_state(values={"updated": "12:34"})
    verdict = compare(exp, mk_state(values=actual), PAIRS, CFG)
    assert not verdict.passed
    assert verdict.detail["value_mismatches"][0]["expected"] == "<VOLATILE>"


def test_clock_mask_does_not_hide_business_text_difference():
    exp = mk_state(texts={"型号 X40PM": 1})
    act = mk_state(texts={"型号 X53PM": 1})
    assert not compare(exp, act, PAIRS, CFG).passed


def test_time_with_meridiem_and_legacy_minute_clock_normalize_equally():
    exp = mk_state(texts={"上次更新时间：40 PM": 1})
    act = mk_state(texts={"上次更新时间：12:53 AM": 1})
    assert compare(exp, act, PAIRS, CFG).passed


# ---------------------------------------------------------------------------
# L0 存活性
# ---------------------------------------------------------------------------

def test_l0_crash_sig():
    v = compare(mk_state(), mk_state(crash_sig="jscrash-com.example-123"), PAIRS, CFG)
    assert not v.passed and v.kind == "L0_CRASH"
    assert v.detail["crash_sig"].startswith("jscrash")


def test_l0_dead_process():
    v = compare(mk_state(), mk_state(alive=False), PAIRS, CFG)
    assert not v.passed and v.kind == "L0_CRASH"
    assert v.detail["failed_predicates"] == ["l0.alive"]


# ---------------------------------------------------------------------------
# L1 页面身份
# ---------------------------------------------------------------------------

def test_l1_wrong_page_heuristic():
    v = compare(mk_state(page="LoginActivity"), mk_state(page="SettingsAbility"),
                PAIRS, CFG)
    assert not v.passed and v.kind == "L1_PAGE"
    assert v.detail["expected_page"] == "LoginActivity"
    assert v.detail["failed_predicates"] == ["l1.page_identity"]


def test_l1_stem_heuristic_pass():
    assert PAIRS.corresponds("LoginActivity", "LoginAbility")
    assert PAIRS.corresponds("com.demo.ProfileActivity", "ProfileAbility:pages/Profile" .split(":")[0])


def test_l1_explicit_mapping_overrides_heuristic():
    pairs = PagePairs(mapping={"LoginActivity": "AuthAbility"})
    # 表中有条目：必须精确等于映射值，词干启发式不再生效
    v = compare(mk_state(page="LoginActivity"), mk_state(page="LoginAbility"),
                pairs, CFG)
    assert not v.passed and v.kind == "L1_PAGE"
    v2 = compare(mk_state(page="LoginActivity"), mk_state(page="AuthAbility"),
                 pairs, CFG)
    assert v2.passed


# ---------------------------------------------------------------------------
# L2 语义内容
# ---------------------------------------------------------------------------

def test_l2_missing_texts():
    exp = mk_state(texts={"欢迎": 1, "登录": 1, "设置": 1, "购物车": 1, "我的": 1})
    act = mk_state(texts={"欢迎": 1, "登录": 1})
    v = compare(exp, act, PAIRS, CFG)
    assert not v.passed and v.kind == "L2_CONTENT"
    assert v.detail["jaccard"] < CFG.jaccard_min
    assert "购物车" in v.detail["missing_texts"]
    assert v.detail["failed_predicates"] == ["l2.text_multiset"]


def test_l2_extra_text_is_diagnostic_only():
    exp = mk_state(texts={"欢迎": 1, "登录": 1})
    act = mk_state(texts={"欢迎": 1, "登录": 1, "系统栏": 1})

    verdict = compare(exp, act, PAIRS, CFG)

    assert verdict.passed
    assert verdict.detail["missing_texts"] == []
    assert verdict.detail["extra_texts"] == ["系统栏"]
    text_predicate = next(
        item for item in verdict.detail["predicates"]
        if item["id"] == "l2.text_multiset"
    )
    assert text_predicate["passed"]
    assert verdict.detail["jaccard"] < CFG.jaccard_min


def test_l2_value_mismatch():
    exp = mk_state(values={"sw_dark": "True"})
    act = mk_state(values={"sw_dark": "False"})
    v = compare(exp, act, PAIRS, CFG)
    assert not v.passed and v.kind == "L2_CONTENT"
    assert v.detail["value_mismatches"] == [
        {"key": "sw_dark", "expected": "True", "actual": "False"}]
    assert v.detail["failed_predicates"] == ["l2.value_state"]


def test_l2_list_empty_nonempty_mismatch():
    exp = mk_state(list_counts={"rv_items": 3})
    act = mk_state(list_counts={"rv_items": 0})
    v = compare(exp, act, PAIRS, CFG)
    assert not v.passed and v.kind == "L2_CONTENT"
    assert v.detail["list_mismatches"][0]["key"] == "rv_items"
    assert v.detail["failed_predicates"] == ["l2.list_state"]


def test_l2_list_count_variance_is_observed_but_not_failed():
    exp = mk_state(list_counts={"rv_items": 3})
    act = mk_state(list_counts={"rv_items": 2})

    verdict = compare(exp, act, PAIRS, CFG)

    assert verdict.passed
    assert verdict.detail["list_mismatches"] == []
    assert verdict.detail["list_observations"] == [{
        "key": "rv_items",
        "matched_key": "rv_items",
        "expected": 3,
        "actual": 2,
        "comparison": "nonempty",
    }]


def test_l2_expected_empty_list_may_be_missing_from_actual():
    exp = mk_state(list_counts={"days_more": 0})
    act = mk_state(list_counts={})

    verdict = compare(exp, act, PAIRS, CFG)

    assert verdict.passed
    assert verdict.detail["list_mismatches"] == []
    assert verdict.detail["list_observations"] == [{
        "key": "days_more",
        "matched_key": None,
        "expected": 0,
        "actual": None,
        "comparison": "expected_empty_missing",
    }]


def test_l2_single_list_key_fallback_records_actual_key():
    exp = mk_state(list_counts={"android_items": 4})
    act = mk_state(list_counts={"harmony_items": 2})

    verdict = compare(exp, act, PAIRS, CFG)

    assert verdict.passed
    assert verdict.detail["list_observations"][0]["comparison"] == "nonempty"
    assert verdict.detail["list_observations"][0]["matched_key"] == "harmony_items"


def test_l2_empty_placeholder_value_is_ignored_when_visible_as_hint():
    hint = "城市中文名或拼音"
    exp = mk_state(
        texts={"请选择城市": 1, hint: 1},
        widgets={"textfield": [hint]},
        values={"cp_search_box": hint},
    )
    act = mk_state(
        texts={"请选择城市": 1, hint: 1},
        widgets={"textfield": [hint]},
        values={"cp_search_box": ""},
    )

    verdict = compare(exp, act, PAIRS, CFG)

    assert verdict.passed
    assert verdict.detail["value_mismatches"] == []
    assert verdict.detail["ignored_placeholders"] == [{
        "key": "cp_search_box",
        "matched_key": "cp_search_box",
        "placeholder": hint,
        "actual": "",
    }]


def test_l2_nonempty_value_mismatch_is_still_strict():
    hint = "请输入城市"
    exp = mk_state(
        texts={hint: 1},
        widgets={"textfield": [hint]},
        values={"search_box": hint},
    )
    act = mk_state(
        texts={hint: 1},
        widgets={"textfield": [hint]},
        values={"search_box": "上海"},
    )

    verdict = compare(exp, act, PAIRS, CFG)

    assert not verdict.passed and verdict.kind == "L2_CONTENT"
    assert verdict.detail["value_mismatches"] == [{
        "key": "search_box",
        "expected": hint,
        "actual": "上海",
    }]


def test_l2_entered_value_does_not_become_placeholder_by_visibility_alone():
    exp = mk_state(
        texts={"hello": 1},
        widgets={"textfield": ["hello"]},
        values={"et_name": "hello"},
    )
    act = mk_state(
        texts={"hello": 1},
        widgets={"textfield": ["hello"]},
        values={"et_name": ""},
    )

    verdict = compare(exp, act, PAIRS, CFG)

    assert not verdict.passed and verdict.kind == "L2_CONTENT"
    assert verdict.detail["value_mismatches"] == [{
        "key": "et_name",
        "expected": "hello",
        "actual": "",
    }]


def test_l2_widget_align_below_threshold():
    exp = mk_state(widgets={"button": ["登录", "注册", "找回密码", "帮助", "关于"]})
    act = mk_state(widgets={"button": ["登录"]})
    v = compare(exp, act, PAIRS, CFG)
    assert not v.passed and v.kind == "L2_CONTENT"
    assert v.detail["widget_align_rate"] < CFG.widget_align_min
    assert v.detail["failed_predicates"] == ["l2.widget_alignment"]


def test_l2_pass_records_all_predicates_as_passed():
    verdict = compare(mk_state(), mk_state(), PAIRS, CFG)

    assert verdict.passed
    assert verdict.detail["failed_predicates"] == []
    assert all(item["passed"] for item in verdict.detail["predicates"])


def test_external_surface_is_an_independent_system_owned_predicate():
    actual = mk_state()
    actual.external_surface = "photo_picker"

    verdict = compare(mk_state(), actual, PAIRS, CFG)

    assert not verdict.passed and verdict.kind == "EXTERNAL_PROTOCOL"
    assert verdict.detail["failed_predicates"] == ["external_protocol.surface"]
    predicate = verdict.detail["external_protocol"]
    assert predicate["ownership"] == "system"
    assert predicate["policy"] == "strict"


# ---------------------------------------------------------------------------
# 子谓词
# ---------------------------------------------------------------------------

def test_multiset_jaccard():
    assert multiset_jaccard({}, {}) == 1.0
    assert multiset_jaccard({"a": 2, "b": 1}, {"a": 1, "b": 1}) == pytest.approx(2 / 3)
    assert multiset_jaccard({"a": 1}, {"b": 1}) == 0.0


def test_widget_align_rate():
    exp = {"button": ["登录", "取消"]}
    act = {"button": ["登录"]}
    assert widget_align_rate(exp, act) == pytest.approx(0.5)
    # 兼容角色：button 的候选可来自 text
    act2 = {"text": ["登录", "取消"]}
    assert widget_align_rate(exp, act2) == pytest.approx(1.0)


def test_compare_keyed_fuzzy_key():
    # 精确 key
    assert compare_keyed({"cb_remember": "False"}, {"cb_remember": "False"}) == []
    # 模糊 key 对齐（两端 id 命名轻微漂移）
    assert compare_keyed({"et_user": "abc"}, {"et_usr": "abc"}) == []
    # 找不到对应 key → 记缺失
    ms = compare_keyed({"et_user": "abc"}, {"totally_different": "abc"})
    assert ms == [{"key": "et_user", "expected": "abc", "actual": None}]

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


# ---------------------------------------------------------------------------
# L1 页面身份
# ---------------------------------------------------------------------------

def test_l1_wrong_page_heuristic():
    v = compare(mk_state(page="LoginActivity"), mk_state(page="SettingsAbility"),
                PAIRS, CFG)
    assert not v.passed and v.kind == "L1_PAGE"
    assert v.detail["expected_page"] == "LoginActivity"


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


def test_l2_value_mismatch():
    exp = mk_state(values={"sw_dark": "True"})
    act = mk_state(values={"sw_dark": "False"})
    v = compare(exp, act, PAIRS, CFG)
    assert not v.passed and v.kind == "L2_CONTENT"
    assert v.detail["value_mismatches"] == [
        {"key": "sw_dark", "expected": "True", "actual": "False"}]


def test_l2_list_count_mismatch():
    exp = mk_state(list_counts={"rv_items": 3})
    act = mk_state(list_counts={"rv_items": 2})
    v = compare(exp, act, PAIRS, CFG)
    assert not v.passed and v.kind == "L2_CONTENT"
    assert v.detail["list_mismatches"][0]["key"] == "rv_items"


def test_l2_widget_align_below_threshold():
    exp = mk_state(widgets={"button": ["登录", "注册", "找回密码", "帮助", "关于"]})
    act = mk_state(widgets={"button": ["登录"]})
    v = compare(exp, act, PAIRS, CFG)
    assert not v.passed and v.kind == "L2_CONTENT"
    assert v.detail["widget_align_rate"] < CFG.widget_align_min


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

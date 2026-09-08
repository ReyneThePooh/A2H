"""M3 对齐器单测：id 保留 / id 丢失 / 纯图标 / 双胞胎控件 四种情形（§6.2）。"""
import pytest

from diff_tester.config import MatcherConfig
from diff_tester.matcher import match
from diff_tester.schemas import TargetFingerprint, UNode

CFG = MatcherConfig()


def mk_node(role, id=None, text="", desc="", rb=(0.1, 0.1, 0.3, 0.2),
            clickable=True, editable=False):
    """rel_bounds → 1000x1000 屏幕上的合成节点。"""
    ab = tuple(int(v * 1000) for v in rb)
    return UNode(role=role, id=id, text=text, desc=desc,
                 abs_bounds=ab, rel_bounds=rb,
                 clickable=clickable, editable=editable, checked=None)


def mk_tree(*nodes):
    return UNode(role="container", id=None, text="", desc="",
                 abs_bounds=(0, 0, 1000, 1000), rel_bounds=(0, 0, 1, 1),
                 clickable=False, editable=False, checked=None,
                 children=list(nodes))


def fp(role="button", id_hint=None, text="", desc="",
       rb=(0.1, 0.8, 0.9, 0.9)):
    return TargetFingerprint(role=role, id_hint=id_hint, text=text,
                             desc=desc, rel_bounds=rb, patch_path=None)


# ---------------------------------------------------------------------------
# 情形 1：翻译器保留了资源 id → id 分量几乎决定性
# ---------------------------------------------------------------------------

def test_id_preserved_matched():
    tree = mk_tree(
        mk_node("button", id="btn_login", text="登录", rb=(0.1, 0.8, 0.9, 0.9)),
        mk_node("button", id="btn_cancel", text="取消", rb=(0.1, 0.1, 0.3, 0.15)),
    )
    m = match(fp(id_hint="btn_login", text="登录"), tree, "CLICK", CFG)
    assert m.kind == "MATCHED"
    assert m.node.id == "btn_login"
    assert m.score > 0.9


# ---------------------------------------------------------------------------
# 情形 2：id 丢失 → 权重按比例摊给 text/role/pos，仍可匹配
# ---------------------------------------------------------------------------

def test_id_lost_weight_redistribution():
    tree = mk_tree(
        mk_node("button", id=None, text="登录", rb=(0.1, 0.8, 0.9, 0.9)),
        mk_node("button", id=None, text="取消", rb=(0.1, 0.1, 0.3, 0.15)),
    )
    m = match(fp(id_hint="btn_login", text="登录"), tree, "CLICK", CFG)
    assert m.kind == "MATCHED"
    assert m.node.text == "登录"
    # text/role/pos 全满分且 id/img 缺失 → 归一化后应为满分
    assert m.score == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 情形 3：纯图标（无 id、无文本、无 patch）→ 依赖 role+pos
# ---------------------------------------------------------------------------

def test_icon_only_position_wins():
    tree = mk_tree(
        mk_node("image", id=None, rb=(0.85, 0.02, 0.95, 0.08)),   # 右上角
        mk_node("image", id=None, rb=(0.05, 0.02, 0.15, 0.08)),   # 左上角
    )
    m = match(fp(role="image", rb=(0.85, 0.02, 0.95, 0.08)), tree, "CLICK", CFG)
    assert m.kind == "MATCHED"
    assert m.node.rel_bounds[0] == pytest.approx(0.85)


# ---------------------------------------------------------------------------
# 情形 4：双胞胎控件（同文本同角色、指纹位置居中）→ AMBIGUOUS
# ---------------------------------------------------------------------------

def test_twin_widgets_ambiguous():
    tree = mk_tree(
        mk_node("button", id=None, text="确定", rb=(0.10, 0.8, 0.45, 0.9)),
        mk_node("button", id=None, text="确定", rb=(0.55, 0.8, 0.90, 0.9)),
    )
    m = match(fp(text="确定", rb=(0.325, 0.8, 0.675, 0.9)), tree, "CLICK", CFG)
    assert m.kind == "AMBIGUOUS"
    assert abs(m.score - m.second_score) < CFG.min_gap


# ---------------------------------------------------------------------------
# UNMAPPED：无候选 / 得分不足
# ---------------------------------------------------------------------------

def test_unmapped_no_candidates():
    tree = mk_tree(mk_node("button", text="登录"))   # 无可编辑控件
    m = match(fp(role="textfield"), tree, "TYPE", CFG)
    assert m.kind == "UNMAPPED"
    assert m.detail.get("reason") == "no_candidates"


def test_unmapped_low_score():
    tree = mk_tree(
        mk_node("button", id="btn_help", text="帮助", rb=(0.1, 0.1, 0.3, 0.2)),
    )
    m = match(fp(id_hint="btn_login", text="登录"), tree, "CLICK", CFG)
    assert m.kind == "UNMAPPED"
    assert m.node is None
    assert m.score < CFG.th_lo


# ---------------------------------------------------------------------------
# 动作过滤：TYPE 只考虑 editable，CLICK 只考虑 clickable
# ---------------------------------------------------------------------------

def test_action_filters_candidates():
    field = mk_node("textfield", id="et_name", clickable=True, editable=True,
                    rb=(0.1, 0.3, 0.9, 0.4))
    btn = mk_node("button", id="btn_ok", text="确定", rb=(0.1, 0.8, 0.9, 0.9))
    tree = mk_tree(field, btn)

    m_type = match(fp(role="textfield", id_hint="et_name", rb=(0.1, 0.3, 0.9, 0.4)),
                   tree, "TYPE", CFG)
    assert m_type.kind == "MATCHED" and m_type.node.id == "et_name"

    m_click = match(fp(id_hint="btn_ok", text="确定"), tree, "CLICK", CFG)
    assert m_click.kind == "MATCHED" and m_click.node.id == "btn_ok"


def test_detail_contains_top_candidates():
    tree = mk_tree(
        mk_node("button", id="a", text="甲", rb=(0.1, 0.1, 0.3, 0.2)),
        mk_node("button", id="b", text="乙", rb=(0.4, 0.4, 0.6, 0.5)),
    )
    m = match(fp(id_hint="a", text="甲", rb=(0.1, 0.1, 0.3, 0.2)), tree, "CLICK", CFG)
    assert "top" in m.detail and len(m.detail["top"]) == 2
    assert m.detail["top"][0]["score"] >= m.detail["top"][1]["score"]

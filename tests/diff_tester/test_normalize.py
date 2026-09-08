"""M1 归一化单测：两端真实格式 dump 样本 → UNode 树字段正确。"""
import json
import os

import pytest

from diff_tester.normalize import parse_android_dump, parse_harmony_dump, find_harmony_page_hint

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture
def android_tree():
    with open(os.path.join(FIXTURES, "android_dump.xml"), encoding="utf-8") as f:
        return parse_android_dump(f.read())


@pytest.fixture
def harmony_tree():
    with open(os.path.join(FIXTURES, "harmony_dump.json"), encoding="utf-8") as f:
        return parse_harmony_dump(json.load(f))


def _by_id(tree, node_id):
    nodes = tree.find_all(lambda n: n.id == node_id)
    assert nodes, f"找不到 id={node_id}"
    return nodes[0]


# ---------------------------------------------------------------------------
# 安卓
# ---------------------------------------------------------------------------

def test_android_root_and_screen(android_tree):
    assert android_tree.role == "container"
    assert android_tree.abs_bounds == (0, 0, 1080, 1920)
    assert android_tree.rel_bounds == (0.0, 0.0, 1.0, 1.0)


def test_android_button(android_tree):
    btn = _by_id(android_tree, "btn_login")
    assert btn.role == "button"
    assert btn.text == "登录"
    assert btn.clickable and not btn.editable
    # resource-id 前缀 "com.example.demo:id/" 已剥离
    assert btn.id == "btn_login"
    # 相对坐标
    x1, y1, x2, y2 = btn.rel_bounds
    assert x1 == pytest.approx(84 / 1080)
    assert y2 == pytest.approx(1782 / 1920)


def test_android_textfield(android_tree):
    et = _by_id(android_tree, "et_username")
    assert et.role == "textfield"
    assert et.editable
    assert et.desc == "用户名"


def test_android_checkbox(android_tree):
    cb = _by_id(android_tree, "cb_remember")
    assert cb.role == "checkbox"
    assert cb.checked is False
    assert cb.text == "记住我"


def test_android_text_and_image(android_tree):
    title = _by_id(android_tree, "tv_title")
    assert title.role == "text" and not title.clickable
    icon = _by_id(android_tree, "btn_settings")
    assert icon.role == "image"
    assert icon.desc == "设置"
    assert icon.clickable


def test_android_list_and_listitem(android_tree):
    rv = _by_id(android_tree, "rv_items")
    assert rv.role == "list"
    assert len(rv.children) == 3
    # 可点击的 LinearLayout 子节点经后处理标记为 listitem
    assert all(c.role == "listitem" for c in rv.children)


def test_android_interactive_count(android_tree):
    inter = list(android_tree.iter_interactive())
    # btn_settings + et_username + cb_remember + 3 个列表行 + btn_login = 7
    assert len(inter) == 7


def test_android_tree_hash_stable(android_tree):
    with open(os.path.join(FIXTURES, "android_dump.xml"), encoding="utf-8") as f:
        again = parse_android_dump(f.read())
    assert android_tree.tree_hash() == again.tree_hash()
    # 改动任一文本 → 哈希变化
    _by_id(again, "btn_login").text = "注册"
    assert android_tree.tree_hash() != again.tree_hash()


# ---------------------------------------------------------------------------
# 鸿蒙
# ---------------------------------------------------------------------------

def test_harmony_roles(harmony_tree):
    assert _by_id(harmony_tree, "btn_login").role == "button"
    assert _by_id(harmony_tree, "et_username").role == "textfield"
    assert _by_id(harmony_tree, "et_username").editable
    assert _by_id(harmony_tree, "cb_remember").role == "checkbox"
    assert _by_id(harmony_tree, "cb_remember").checked is False
    assert _by_id(harmony_tree, "tv_title").role == "text"
    assert _by_id(harmony_tree, "btn_settings").role == "image"


def test_harmony_list(harmony_tree):
    lst = _by_id(harmony_tree, "rv_items")
    assert lst.role == "list"
    assert len(lst.children) == 3
    assert all(c.role == "listitem" for c in lst.children)


def test_harmony_bounds_rel(harmony_tree):
    btn = _by_id(harmony_tree, "btn_login")
    assert btn.abs_bounds == (84, 1650, 996, 1782)
    assert btn.rel_bounds[0] == pytest.approx(84 / 1080)


def test_harmony_page_hint():
    with open(os.path.join(FIXTURES, "harmony_dump.json"), encoding="utf-8") as f:
        data = json.load(f)
    assert find_harmony_page_hint(data) == "pages/Index"


def test_unode_roundtrip(android_tree):
    from diff_tester.schemas import UNode
    d = android_tree.to_dict()
    rebuilt = UNode.from_dict(json.loads(json.dumps(d)))
    assert rebuilt.tree_hash() == android_tree.tree_hash()

"""状态抽象与掩码单测（§6.1）。"""
import json
import os

import pytest

from diff_tester.normalize import parse_android_dump
from diff_tester.state import (build_state_vector, compile_masks, mask_text,
                               page_fingerprint, pair_pages)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
MASKS = compile_masks()


# ---------------------------------------------------------------------------
# 掩码
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expect_masked", [
    ("12:34", True),
    ("23:59:59", True),
    ("2026-09-04", True),
    ("2026年9月4日", True),
    ("9/4", True),
    ("123456", True),          # >=6 位数字长串
    ("50%", True),
    ("登录", False),
    ("hello world 42", False),  # 短数字不掩码
])
def test_mask_text(raw, expect_masked):
    out = mask_text(raw, MASKS)
    if expect_masked:
        assert "<VOLATILE>" in out
    else:
        assert out == raw


def test_mask_inside_sentence():
    assert mask_text("上次登录 12:34", MASKS) == "上次登录 <VOLATILE>"


# ---------------------------------------------------------------------------
# build_state_vector
# ---------------------------------------------------------------------------

@pytest.fixture
def tree():
    with open(os.path.join(FIXTURES, "android_dump.xml"), encoding="utf-8") as f:
        return parse_android_dump(f.read())


def test_state_vector_fields(tree):
    sv = build_state_vector(tree, "MainActivity", MASKS)
    assert sv.page == "MainActivity"
    # 文本 multiset：可见 text 与 desc 都计入
    assert sv.texts["我的应用"] == 1
    assert sv.texts["登录"] == 1
    assert sv.texts["设置"] == 1        # 来自 content-desc
    # values：输入框内容 + 复选框状态
    assert sv.values["et_username"] == ""
    assert sv.values["cb_remember"] == "False"
    # 列表条目数
    assert sv.list_counts["rv_items"] == 3
    # widgets：交互控件按 role 分组
    assert "登录" in sv.widgets["button"]
    assert len(sv.widgets["listitem"]) == 3


def test_state_hash_deterministic(tree):
    sv1 = build_state_vector(tree, "MainActivity", MASKS)
    sv2 = build_state_vector(tree, "MainActivity", MASKS)
    assert sv1.hash() == sv2.hash()
    sv3 = build_state_vector(tree, "OtherActivity", MASKS)
    assert sv1.hash() != sv3.hash()


def test_state_hash_ignores_dict_order(tree):
    sv = build_state_vector(tree, "MainActivity", MASKS)
    reordered = dict(reversed(list(sv.texts.items())))
    sv.texts = reordered
    sv_again = build_state_vector(tree, "MainActivity", MASKS)
    assert sv.hash() == sv_again.hash()


def test_volatile_text_stable_hash(tree):
    """同一界面时钟跳动 → 掩码后哈希不变。"""
    import copy
    t2 = copy.deepcopy(tree)
    title = t2.find_all(lambda n: n.id == "tv_title")[0]
    title.text = "12:34"
    t3 = copy.deepcopy(tree)
    t3.find_all(lambda n: n.id == "tv_title")[0].text = "12:35"
    sv2 = build_state_vector(t2, "MainActivity", MASKS)
    sv3 = build_state_vector(t3, "MainActivity", MASKS)
    assert sv2.hash() == sv3.hash()


# ---------------------------------------------------------------------------
# 页面指纹与自动配对
# ---------------------------------------------------------------------------

def test_page_fingerprint(tree):
    fp = page_fingerprint(tree)
    assert "我的应用" in fp["title_texts"]     # 标题区（顶部 15%）
    assert fp["role_counts"]["listitem"] == 3


def test_pair_pages(tree):
    fp = page_fingerprint(tree)
    other = {"title_texts": ["完全不同的页面"], "role_counts": {"image": 20}}
    mapping = pair_pages(
        {"MainActivity": fp, "GalleryActivity": other},
        {"MainAbility": fp, "GalleryAbility": other},
    )
    assert mapping["MainActivity"] == "MainAbility"
    assert mapping["GalleryActivity"] == "GalleryAbility"

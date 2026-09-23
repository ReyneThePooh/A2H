"""状态抽象与掩码单测（§6.1）。"""
import json
import os

import pytest

from diff_tester.normalize import parse_android_dump, parse_harmony_dump
from diff_tester.schemas import ACTIVE_SCOPE_ID, UNode
from diff_tester.state import (UNCONFIRMED_EXTERNAL_SURFACE,
                               build_state_vector, compile_masks, mask_text,
                               page_fingerprint, pair_pages)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
MASKS = compile_masks()


# ---------------------------------------------------------------------------
# 掩码
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expect_masked", [
    ("12:34", True),
    ("23:59:59", True),
    ("上次更新时间：40 PM", True),
    ("updated 7 am", True),
    ("updated 12:34 PM", True),
    ("型号 X40PM", False),
    ("型号 X40 PM", False),
    ("数量 40 PM", False),
    ("上次更新时间：99 PM", False),
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
    assert mask_text("上次更新时间：40 PM", MASKS) == "上次更新时间：<VOLATILE>"
    assert mask_text("上次更新时间：12:40 PM", MASKS) == "上次更新时间：<VOLATILE>"


def test_empty_mask_configuration_disables_masking():
    assert mask_text("上次更新时间：40 PM", compile_masks([])) == "上次更新时间：40 PM"


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


def test_clickable_harmony_image_is_summarized_as_button():
    with open(os.path.join(FIXTURES, "harmony_dump.json"), encoding="utf-8") as f:
        harmony_tree = parse_harmony_dump(json.load(f))

    sv = build_state_vector(harmony_tree, "pages/Index", MASKS)

    assert "设置" in sv.widgets["button"]
    assert "image" not in sv.widgets


def test_non_clickable_image_is_not_summarized_as_button():
    image = UNode(
        role="image", id="cover", text="", desc="封面",
        abs_bounds=(0, 0, 100, 100), rel_bounds=(0.0, 0.0, 0.1, 0.1),
        clickable=False, editable=False, checked=None,
    )

    sv = build_state_vector(image, "pages/Index", MASKS)

    assert sv.widgets == {}


def test_explicit_active_scope_excludes_background_page_state():
    background = UNode(
        role="button", id="background_action", text="页面操作", desc="",
        abs_bounds=(0, 0, 1000, 1800), rel_bounds=(0.0, 0.0, 1.0, 1.0),
        clickable=True, editable=False, checked=None,
    )
    menu_action = UNode(
        role="button", id="menu_action", text="菜单操作", desc="",
        abs_bounds=(700, 100, 950, 250), rel_bounds=(0.7, 0.05, 0.95, 0.125),
        clickable=True, editable=False, checked=None,
    )
    menu = UNode(
        role="container", id=ACTIVE_SCOPE_ID, text="", desc="",
        abs_bounds=(650, 50, 1000, 350), rel_bounds=(0.65, 0.025, 1.0, 0.175),
        clickable=False, editable=False, checked=None, children=[menu_action],
    )
    root = UNode(
        role="container", id=None, text="", desc="",
        abs_bounds=(0, 0, 1000, 2000), rel_bounds=(0.0, 0.0, 1.0, 1.0),
        clickable=False, editable=False, checked=None,
        children=[background, menu],
    )

    sv = build_state_vector(root, "pages/Main", MASKS)

    assert sv.texts == {"菜单操作": 1}
    assert sv.widgets == {"button": ["菜单操作"]}


def test_photo_picker_is_detected_and_excluded_from_application_state():
    app_button = UNode(
        role="button", id="bg_img", text="本地照片", desc="",
        abs_bounds=(0, 0, 100, 100), rel_bounds=(0, 0, 0.2, 0.1),
        clickable=True, editable=False, checked=None,
    )
    picker_content = UNode(
        role="container", id="photo_grid_base", text="", desc="",
        abs_bounds=(0, 0, 1000, 2000), rel_bounds=(0, 0, 1, 1),
        clickable=False, editable=False, checked=None,
        children=[
            UNode("button", None, "所有图片", "", (0, 0, 1, 1),
                  (0, 0, 0.2, 0.1), True, False, None),
            UNode("button", None, "所有相册", "", (0, 0, 1, 1),
                  (0, 0, 0.2, 0.1), True, False, None),
            UNode("text", None, "安全访问图库", "", (0, 0, 1, 1),
                  (0, 0, 0.2, 0.1), False, False, None),
        ],
    )
    picker = UNode(
        role="list", id=None, text="", desc="",
        abs_bounds=(0, 0, 1000, 2000), rel_bounds=(0, 0, 1, 1),
        clickable=True, editable=False, checked=None,
        children=[picker_content],
    )
    root = UNode("container", None, "", "", (0, 0, 1000, 2000),
                 (0, 0, 1, 1), False, False, None,
                 children=[app_button, picker])

    state = build_state_vector(root, "pages/ChangeWallpaper", MASKS)

    assert state.external_surface == "photo_picker"
    assert state.texts == {"本地照片": 1}
    assert state.widgets == {"button": ["本地照片"]}
    assert state.list_counts == {}


@pytest.mark.parametrize("root_id", [
    "photo_grid_base",
    "com.ohos.photos:id/photo_grid_base",
    "/system/photopicker/layout/photo_grid_base",
])
def test_photo_picker_root_id_is_namespace_safe_and_locale_independent(root_id):
    picker = UNode(
        role="container", id=root_id, text="", desc="",
        abs_bounds=(0, 0, 1000, 2000), rel_bounds=(0, 0, 1, 1),
        clickable=False, editable=False, checked=None,
        children=[
            UNode("image", "thumbnail", "", "", (0, 0, 500, 500),
                  (0, 0, 0.5, 0.25), True, False, None),
        ],
    )

    state = build_state_vector(picker, "system.photo_picker", MASKS)

    assert state.external_surface == "photo_picker"
    assert state.texts == {}
    assert state.widgets == {}


def test_photo_picker_labels_without_platform_root_remain_application_state():
    root = UNode(
        role="container", id=None, text="", desc="",
        abs_bounds=(0, 0, 1000, 2000), rel_bounds=(0, 0, 1, 1),
        clickable=False, editable=False, checked=None,
        children=[
            UNode("button", None, "所有图片", "", (0, 0, 1, 1),
                  (0, 0, 0.2, 0.1), True, False, None),
            UNode("button", None, "所有相册", "", (0, 0, 1, 1),
                  (0, 0, 0.2, 0.1), True, False, None),
            UNode("text", None, "安全访问图库", "", (0, 0, 1, 1),
                  (0, 0, 0.2, 0.1), False, False, None),
        ],
    )

    state = build_state_vector(root, "pages/Main", MASKS)

    assert state.external_surface == UNCONFIRMED_EXTERNAL_SURFACE
    assert state.texts == {"所有图片": 1, "所有相册": 1, "安全访问图库": 1}
    assert state.widgets == {"button": ["所有图片", "所有相册"]}


def test_photo_picker_markers_do_not_promote_an_unrecognized_root():
    platform_root = UNode(
        role="container", id="app/photo_grid_base_preview", text="", desc="",
        abs_bounds=(0, 0, 1000, 1000), rel_bounds=(0, 0, 1, 0.5),
        clickable=False, editable=False, checked=None,
        children=[
            UNode("button", None, "所有图片", "", (0, 0, 1, 1),
                  (0, 0, 0.2, 0.1), True, False, None),
        ],
    )
    app_label = UNode(
        role="text", id="album_help", text="所有相册", desc="安全访问图库",
        abs_bounds=(0, 1000, 1000, 2000), rel_bounds=(0, 0.5, 1, 1),
        clickable=False, editable=False, checked=None,
    )
    root = UNode("container", None, "", "", (0, 0, 1000, 2000),
                 (0, 0, 1, 1), False, False, None,
                 children=[platform_root, app_label])

    state = build_state_vector(root, "pages/Main", MASKS)

    assert state.external_surface == UNCONFIRMED_EXTERNAL_SURFACE
    assert state.texts == {"所有图片": 1, "所有相册": 1, "安全访问图库": 1}


def test_unknown_system_like_text_is_not_tolerated_or_removed():
    root = UNode("container", None, "未知系统提示", "", (0, 0, 100, 100),
                 (0, 0, 1, 1), False, False, None)

    state = build_state_vector(root, "pages/Main", MASKS)

    assert state.external_surface is None
    assert state.texts == {"未知系统提示": 1}


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

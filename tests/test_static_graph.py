"""静态依赖层测试 — 不依赖 LLM，纯确定性逻辑

覆盖：类型引用/Intent 提取、文件级依赖图、Java↔XML 硬绑定、
SCC 拓扑排序、Unit 修复（硬约束执行）、结果校验。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from analyzers.static import JavaStaticAnalyzer, analyze_file
from pipeline.static_graph import (
    norm_path, ProjectIndex, build_file_graph, build_hard_groups,
    tarjan_scc, layered_topological_sort, validate_plan,
)
from pipeline.order_determiner import Unit, UnitBuilder, UnitDependencyAnalyzer, topological_sort, merge_cyclic_units


# ============================================================
# 合成迷你项目（同包、无项目内 import — 模拟真实痛点场景）
# ============================================================

MAIN_ACTIVITY = """
package com.example.app;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;

public class MainActivity extends Activity {
    private DbHelper db;
    private TaskAdapter adapter;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);
        db = new DbHelper(this);
        adapter = new TaskAdapter(this);
        startActivity(new Intent(this, DetailActivity.class));
    }
}
"""

DETAIL_ACTIVITY = """
package com.example.app;

import android.app.Activity;
import android.os.Bundle;

public class DetailActivity extends Activity {
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_detail);
        String table = DbHelper.TABLE_NAME;
    }
}
"""

DB_HELPER = """
package com.example.app;

public class DbHelper {
    public static final String TABLE_NAME = "tasks";
    public DbHelper(Object context) {}
}
"""

TASK_ADAPTER = """
package com.example.app;

import android.view.LayoutInflater;
import android.view.View;
import android.view.ViewGroup;
import android.widget.BaseAdapter;

public class TaskAdapter extends BaseAdapter {
    public TaskAdapter(Object context) {}

    public View getView(int position, View convertView, ViewGroup parent) {
        return LayoutInflater.from(null).inflate(R.layout.item_task, parent, false);
    }

    public int getCount() { return 0; }
    public Object getItem(int position) { return null; }
    public long getItemId(int position) { return 0; }
}
"""

ACTIVITY_MAIN_XML = """<?xml version="1.0" encoding="utf-8"?>
<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android"
    android:orientation="vertical"
    android:layout_width="match_parent"
    android:layout_height="match_parent">
    <include layout="@layout/header" />
    <ListView android:id="@+id/task_list"
        android:layout_width="match_parent"
        android:layout_height="wrap_content" />
</LinearLayout>
"""

ACTIVITY_DETAIL_XML = """<?xml version="1.0" encoding="utf-8"?>
<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android"
    android:layout_width="match_parent"
    android:layout_height="match_parent">
    <TextView android:id="@+id/detail_text"
        android:layout_width="wrap_content"
        android:layout_height="wrap_content" />
</LinearLayout>
"""

ITEM_TASK_XML = """<?xml version="1.0" encoding="utf-8"?>
<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android"
    android:layout_width="match_parent"
    android:layout_height="wrap_content">
    <TextView android:id="@+id/task_title"
        android:layout_width="wrap_content"
        android:layout_height="wrap_content" />
</LinearLayout>
"""

HEADER_XML = """<?xml version="1.0" encoding="utf-8"?>
<TextView xmlns:android="http://schemas.android.com/apk/res/android"
    android:id="@+id/header_title"
    android:layout_width="match_parent"
    android:layout_height="wrap_content" />
"""

FILES = {
    "java/com/example/app/MainActivity.java": MAIN_ACTIVITY,
    "java/com/example/app/DetailActivity.java": DETAIL_ACTIVITY,
    "java/com/example/app/DbHelper.java": DB_HELPER,
    "java/com/example/app/TaskAdapter.java": TASK_ADAPTER,
    "res/layout/activity_main.xml": ACTIVITY_MAIN_XML,
    "res/layout/activity_detail.xml": ACTIVITY_DETAIL_XML,
    "res/layout/item_task.xml": ITEM_TASK_XML,
    "res/layout/header.xml": HEADER_XML,
}


def make_summaries():
    return {path: analyze_file(path, code) for path, code in FILES.items()}


# ============================================================
# 静态提取
# ============================================================

def test_type_references_same_package():
    """同包无 import 的类型引用必须被提取到"""
    s = JavaStaticAnalyzer.analyze("MainActivity.java", MAIN_ACTIVITY)
    assert "DbHelper" in s.type_references
    assert "TaskAdapter" in s.type_references
    assert "MainActivity" not in s.type_references  # 不含自身


def test_static_member_qualifier():
    """静态成员访问 DbHelper.TABLE_NAME 应识别 DbHelper"""
    s = JavaStaticAnalyzer.analyze("DetailActivity.java", DETAIL_ACTIVITY)
    assert "DbHelper" in s.type_references


def test_intent_targets():
    s = JavaStaticAnalyzer.analyze("MainActivity.java", MAIN_ACTIVITY)
    assert s.intent_targets == ["DetailActivity"]


def test_layout_refs():
    s = JavaStaticAnalyzer.analyze("TaskAdapter.java", TASK_ADAPTER)
    assert s.resource_refs.get("layout") == ["item_task"]


# ============================================================
# 文件级依赖图
# ============================================================

def test_file_graph_edges():
    graph = build_file_graph(make_summaries())
    main = graph["java/com/example/app/MainActivity.java"]

    assert "java/com/example/app/DbHelper.java" in main          # 同包类型引用
    assert "java/com/example/app/TaskAdapter.java" in main       # 同包类型引用
    assert "java/com/example/app/DetailActivity.java" in main    # Intent 跳转
    assert "res/layout/activity_main.xml" in main                # 布局引用

    detail = graph["java/com/example/app/DetailActivity.java"]
    assert "java/com/example/app/DbHelper.java" in detail        # 静态成员访问

    main_xml = graph["res/layout/activity_main.xml"]
    assert "res/layout/header.xml" in main_xml                   # include


def test_file_graph_windows_paths():
    """Windows 反斜杠路径也应正常构图"""
    summaries = {p.replace('/', '\\'): analyze_file(p, c) for p, c in FILES.items()}
    graph = build_file_graph(summaries)
    main = graph["java/com/example/app/MainActivity.java"]
    assert "java/com/example/app/DbHelper.java" in main


# ============================================================
# 硬绑定分组
# ============================================================

def test_hard_groups():
    groups = build_hard_groups(make_summaries())
    by_member = {}
    for g in groups:
        for f in g:
            by_member[f] = set(g)

    # MainActivity 与它的布局同组；布局 include 的 header 也在组内
    main_group = by_member["java/com/example/app/MainActivity.java"]
    assert "res/layout/activity_main.xml" in main_group
    assert "res/layout/header.xml" in main_group

    # Adapter 与 item 布局同组
    adapter_group = by_member["java/com/example/app/TaskAdapter.java"]
    assert "res/layout/item_task.xml" in adapter_group

    # DbHelper 不引用布局，不应出现在任何硬绑定组
    assert "java/com/example/app/DbHelper.java" not in by_member


# ============================================================
# SCC 与拓扑排序
# ============================================================

def test_tarjan_scc_cycle():
    deps = {"A": {"B"}, "B": {"A"}, "C": {"A"}}
    sccs = tarjan_scc(["A", "B", "C"], deps)
    scc_sets = [set(s) for s in sccs]
    assert {"A", "B"} in scc_sets
    assert {"C"} in scc_sets


def test_layered_sort_dag():
    # C 依赖 A、B；D 依赖 C
    deps = {"C": {"A", "B"}, "D": {"C"}}
    layers, cycles = layered_topological_sort(["A", "B", "C", "D"], deps)
    assert cycles == []
    layer_of = {n: i for i, layer in enumerate(layers) for n in layer}
    assert layer_of["A"] == 0 and layer_of["B"] == 0
    assert layer_of["C"] == 1
    assert layer_of["D"] == 2


def test_layered_sort_cycle_and_downstream():
    """环内成员同层；依赖环的下游节点必须排在环之后（旧实现的 bug 场景）"""
    deps = {"A": {"B"}, "B": {"A"}, "C": {"A"}, "D": {"C"}}
    layers, cycles = layered_topological_sort(["A", "B", "C", "D"], deps)
    assert len(cycles) == 1 and set(cycles[0]) == {"A", "B"}
    layer_of = {n: i for i, layer in enumerate(layers) for n in layer}
    assert layer_of["A"] == layer_of["B"]
    assert layer_of["C"] > layer_of["A"]
    assert layer_of["D"] > layer_of["C"]


def test_topological_sort_units():
    units = [Unit(name=n, sources=[f"{n}.java"]) for n in ["A", "B", "C"]]
    deps = {"C": {"A", "B"}, "A": set(), "B": set()}
    layers, cycles = topological_sort(units, deps)
    assert cycles == []
    assert {u.name for u in layers[0]} == {"A", "B"}
    assert [u.name for u in layers[1]] == ["C"]


def test_merge_cycle_preserves_sources_external_edges_and_is_idempotent():
    units = [Unit(name=n, sources=[f"{n}.java"], description=n) for n in ("A", "B", "Base", "Next")]
    deps = {"A": {"B", "Base"}, "B": {"A"}, "Next": {"A", "B"}}
    merged, edges = merge_cyclic_units(units, deps)
    group = next(unit for unit in merged if len(unit.sources) == 2)
    assert group.sources == ["A.java", "B.java"]
    assert edges[group.name] == {"Base"} and edges["Next"] == {group.name}
    assert next(unit for unit in merged if unit.name == "Base") is units[2]
    layers, cycles = topological_sort(merged, edges)
    assert not cycles and [unit.name for unit in layers[1]] == [group.name]
    again, again_edges = merge_cyclic_units(merged, edges)
    assert again_edges == edges and {u.name for u in again} == {u.name for u in merged}


# ============================================================
# Unit 依赖分析（静态投影，不走 LLM）
# ============================================================

def test_unit_dependency_static_projection():
    summaries = make_summaries()
    units = [
        Unit(name="主页", sources=[
            "java/com/example/app/MainActivity.java",
            "java/com/example/app/TaskAdapter.java",
            "res/layout/activity_main.xml",
            "res/layout/header.xml",
            "res/layout/item_task.xml",
        ]),
        Unit(name="详情页", sources=[
            "java/com/example/app/DetailActivity.java",
            "res/layout/activity_detail.xml",
        ]),
        Unit(name="数据库", sources=["java/com/example/app/DbHelper.java"]),
    ]
    analyzer = UnitDependencyAnalyzer(llm=None, tools=None, summaries=summaries)
    deps = analyzer.analyze(units, use_llm=False)

    assert deps["主页"] == {"详情页", "数据库"}
    assert deps["详情页"] == {"数据库"}
    assert deps["数据库"] == set()


# ============================================================
# Unit 修复：硬绑定约束执行
# ============================================================

def test_repair_units_enforces_hard_groups():
    builder = UnitBuilder(llm=None, tools=None)
    all_files = list(FILES.keys())
    hard_groups = build_hard_groups(make_summaries())

    # 模拟 LLM 把 Adapter 和它的 item 布局拆到了两个单元
    units = [
        Unit(name="主页", sources=[
            "java/com/example/app/MainActivity.java",
            "java/com/example/app/TaskAdapter.java",
            "res/layout/activity_main.xml",
            "res/layout/header.xml",
        ]),
        Unit(name="孤儿布局", sources=["res/layout/item_task.xml"]),
        Unit(name="详情页", sources=[
            "java/com/example/app/DetailActivity.java",
            "res/layout/activity_detail.xml",
        ]),
        Unit(name="数据库", sources=["java/com/example/app/DbHelper.java"]),
    ]
    repaired = builder._repair_units(units, all_files, hard_groups)

    file_to_unit = {f: u.name for u in repaired for f in u.sources}
    # item_task.xml 被并回 Adapter 所在单元
    assert file_to_unit["res/layout/item_task.xml"] == \
        file_to_unit["java/com/example/app/TaskAdapter.java"]
    # 空单元被清理
    assert all(u.sources for u in repaired)
    # 校验通过
    violations = validate_plan(
        repaired,
        {u.name: set() for u in repaired},
        [repaired],
        all_files,
        hard_groups,
    )
    assert violations == []


def test_repair_units_dedup_and_missing():
    builder = UnitBuilder(llm=None, tools=None)
    all_files = ["A.java", "B.java", "C.java"]
    units = [
        Unit(name="U1", sources=["A.java", "B.java"]),
        Unit(name="U2", sources=["B.java"]),  # 重复分配
        # C.java 遗漏
    ]
    repaired = builder._repair_units(units, all_files, hard_groups=[])
    file_to_unit = {f: u.name for u in repaired for f in u.sources}
    assert file_to_unit["B.java"] == "U1"
    assert "C.java" in file_to_unit


def test_normalize_sources():
    all_files = ["java/com/example/app/MainActivity.java"]
    out = UnitBuilder._normalize_sources(
        ["java\\com\\example\\app\\MainActivity.java", "MainActivity.java", "Ghost.java"],
        all_files,
    )
    # 反斜杠与结尾匹配都能命中；幻觉路径被丢弃
    assert out == [
        "java/com/example/app/MainActivity.java",
        "java/com/example/app/MainActivity.java",
    ]


# ============================================================
# 结果校验
# ============================================================

def test_validate_plan_detects_violations():
    units = [Unit(name="U1", sources=["A.java"]), Unit(name="U2", sources=["B.xml"])]
    deps = {"U1": {"U2"}}
    # 错误分层：U1 依赖 U2 却排在 U2 前面
    layers = [[units[0]], [units[1]]]
    violations = validate_plan(units, deps, layers, ["A.java", "B.xml", "C.java"],
                               hard_groups=[["A.java", "B.xml"]])
    text = "\n".join(violations)
    assert "未分配" in text        # C.java 未覆盖
    assert "拓扑序违规" in text     # U1 在 U2 之前
    assert "硬绑定组被拆散" in text


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))

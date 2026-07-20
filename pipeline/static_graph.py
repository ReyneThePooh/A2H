"""确定性静态依赖层 — 文件级依赖图、Java↔XML 硬绑定、SCC 拓扑排序

设计目标：能静态确定的事实（类型引用、Intent 跳转、布局绑定、include 关系）
全部用确定性规则提取，LLM 只负责审核和补充真正模糊的部分。
"""

from pathlib import Path
from collections import defaultdict

from analyzers.static import FileSummary, XmlSummary


def norm_path(p: str) -> str:
    """统一路径分隔符为 '/'，避免 Windows '\\' 导致匹配失败"""
    return str(p).replace('\\', '/')


# ============================================================
# 项目索引 — FQCN / 类名 / 布局名的精确解析
# ============================================================

class ProjectIndex:
    """基于摘要构建的项目级索引，支持精确的类名/布局名解析"""

    def __init__(self, summaries: dict[str, FileSummary | XmlSummary]):
        self.java: dict[str, FileSummary] = {}
        self.xml: dict[str, XmlSummary] = {}
        self.fqcn_to_file: dict[str, str] = {}
        self.class_name_to_files: dict[str, list[str]] = defaultdict(list)
        self.layout_name_to_file: dict[str, str] = {}

        for raw_path, s in summaries.items():
            path = norm_path(raw_path)
            if isinstance(s, FileSummary):
                self.java[path] = s
                if s.class_name:
                    fqcn = f"{s.package}.{s.class_name}" if s.package else s.class_name
                    self.fqcn_to_file[fqcn] = path
                    self.class_name_to_files[s.class_name].append(path)
            elif isinstance(s, XmlSummary):
                self.xml[path] = s
                if s.xml_type == 'layout':
                    self.layout_name_to_file[Path(path).stem] = path

    def resolve_import(self, fqcn: str) -> str | None:
        """精确解析 import 的 FQCN 到文件路径；失败时回退到唯一类名匹配"""
        if fqcn in self.fqcn_to_file:
            return self.fqcn_to_file[fqcn]
        simple = fqcn.rsplit('.', 1)[-1]
        return self.resolve_class(simple)

    def resolve_class(self, simple_name: str, from_package: str = "") -> str | None:
        """按类简单名解析到文件路径；重名类优先同包"""
        candidates = self.class_name_to_files.get(simple_name, [])
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1 and from_package:
            same_pkg = [p for p in candidates if self.java[p].package == from_package]
            if len(same_pkg) == 1:
                return same_pkg[0]
        return candidates[0] if candidates else None

    def resolve_layout(self, layout_name: str) -> str | None:
        return self.layout_name_to_file.get(layout_name)


# ============================================================
# 文件级依赖图
# ============================================================

def build_file_graph(summaries: dict[str, FileSummary | XmlSummary]) -> dict[str, set[str]]:
    """构建文件级依赖图：{文件: 它依赖的文件集合}

    边来源（全部确定性）：
    - Java import 中的项目内 FQCN
    - Java 类型引用（字段/参数/返回值/new/extends/静态调用）→ 覆盖同包无 import 的场景
    - Intent 跳转目标（new Intent(..., Xxx.class)）
    - Java → 其引用的 layout XML（R.layout.x）
    - layout XML → 其 <include> 的子布局（@layout/x）
    """
    index = ProjectIndex(summaries)
    graph: dict[str, set[str]] = {norm_path(p): set() for p in summaries}

    for path, s in index.java.items():
        deps = graph[path]

        for imp in s.project_imports:
            target = index.resolve_import(imp)
            if target and target != path:
                deps.add(target)

        for ref in s.type_references:
            target = index.resolve_class(ref, from_package=s.package)
            if target and target != path:
                deps.add(target)

        for target_class in s.intent_targets:
            target = index.resolve_class(target_class, from_package=s.package)
            if target and target != path:
                deps.add(target)

        for layout_name in s.resource_refs.get('layout', []):
            target = index.resolve_layout(layout_name)
            if target and target != path:
                deps.add(target)

    for path, s in index.xml.items():
        deps = graph[path]
        for layout_name in s.resource_refs.get('layout', []):
            target = index.resolve_layout(layout_name)
            if target and target != path:
                deps.add(target)

    return graph


# ============================================================
# Java ↔ XML 硬绑定分组（union-find）
# ============================================================

def build_hard_groups(summaries: dict[str, FileSummary | XmlSummary]) -> list[list[str]]:
    """确定性预分组：引用同一布局的 Java 与该布局强制同组；include 的子布局随父布局同组。

    返回大小 >= 2 的分组列表（单文件不构成约束）。
    """
    index = ProjectIndex(summaries)
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for path, s in index.java.items():
        for layout_name in s.resource_refs.get('layout', []):
            target = index.resolve_layout(layout_name)
            if target:
                union(path, target)

    for path, s in index.xml.items():
        if s.xml_type != 'layout':
            continue
        for layout_name in s.resource_refs.get('layout', []):
            target = index.resolve_layout(layout_name)
            if target:
                union(path, target)

    groups: dict[str, list[str]] = defaultdict(list)
    for node in parent:
        groups[find(node)].append(node)

    return [sorted(g) for g in groups.values() if len(g) >= 2]


# ============================================================
# Tarjan SCC + 分层拓扑排序
# ============================================================

def tarjan_scc(nodes: list[str], deps: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan 强连通分量（迭代实现，避免递归深度限制）"""
    node_set = set(nodes)
    index_counter = [0]
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    sccs: list[list[str]] = []

    for root in nodes:
        if root in indices:
            continue
        work = [(root, iter(sorted(deps.get(root, set()) & node_set)))]
        indices[root] = lowlink[root] = index_counter[0]
        index_counter[0] += 1
        stack.append(root)
        on_stack.add(root)

        while work:
            v, it = work[-1]
            advanced = False
            for w in it:
                if w not in indices:
                    indices[w] = lowlink[w] = index_counter[0]
                    index_counter[0] += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, iter(sorted(deps.get(w, set()) & node_set))))
                    advanced = True
                    break
                elif w in on_stack:
                    lowlink[v] = min(lowlink[v], indices[w])
            if advanced:
                continue

            work.pop()
            if work:
                u = work[-1][0]
                lowlink[u] = min(lowlink[u], lowlink[v])
            if lowlink[v] == indices[v]:
                scc = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    scc.append(w)
                    if w == v:
                        break
                sccs.append(sorted(scc))

    return sccs


def layered_topological_sort(
    names: list[str], deps: dict[str, set[str]]
) -> tuple[list[list[str]], list[list[str]]]:
    """SCC 缩点后的分层拓扑排序。

    返回 (layers, cycles)：
    - layers: 每层是可并行处理的名字列表；任意节点的所有依赖都在更早的层
    - cycles: 检测到的循环依赖分组（len >= 2 的 SCC），调用方应把环内成员放进同一翻译上下文
    """
    sccs = tarjan_scc(names, deps)
    node_to_scc: dict[str, int] = {}
    for i, scc in enumerate(sccs):
        for n in scc:
            node_to_scc[n] = i

    # 缩点后的 DAG
    scc_deps: dict[int, set[int]] = {i: set() for i in range(len(sccs))}
    for n in names:
        for d in deps.get(n, set()):
            if d not in node_to_scc:
                continue
            si, sj = node_to_scc[n], node_to_scc[d]
            if si != sj:
                scc_deps[si].add(sj)

    # 对 DAG 做最长路径分层：depth = max(依赖的 depth) + 1
    depth: dict[int, int] = {}

    def compute_depth(i: int) -> int:
        if i in depth:
            return depth[i]
        depth[i] = 0  # 占位防御（DAG 上不会成环）
        d = 0
        for j in scc_deps[i]:
            d = max(d, compute_depth(j) + 1)
        depth[i] = d
        return d

    for i in range(len(sccs)):
        compute_depth(i)

    max_depth = max(depth.values()) if depth else 0
    layers: list[list[str]] = [[] for _ in range(max_depth + 1)]
    for i, scc in enumerate(sccs):
        layers[depth[i]].extend(scc)

    cycles = [scc for scc in sccs if len(scc) >= 2]
    return layers, cycles


# ============================================================
# 结果校验（不变量）
# ============================================================

def validate_plan(
    units: list,  # list[Unit]
    unit_deps: dict[str, set[str]],
    layers: list[list],  # list[list[Unit]]
    all_files: list[str],
    hard_groups: list[list[str]],
) -> list[str]:
    """校验划分与排序结果的不变量，返回违规描述列表（空列表 = 通过）"""
    violations: list[str] = []
    unit_names = {u.name for u in units}

    # 1. 每个文件恰好属于一个 unit
    file_count: dict[str, int] = defaultdict(int)
    for u in units:
        for src in u.sources:
            file_count[norm_path(src)] += 1
    for f in all_files:
        f = norm_path(f)
        if file_count.get(f, 0) == 0:
            violations.append(f"文件未分配到任何单元: {f}")
        elif file_count[f] > 1:
            violations.append(f"文件被分配到 {file_count[f]} 个单元: {f}")

    # 2. 依赖边两端必须是存在的 unit
    for name, dep_set in unit_deps.items():
        if name not in unit_names:
            violations.append(f"依赖图中存在未知单元: {name}")
        for d in dep_set:
            if d not in unit_names:
                violations.append(f"单元 {name} 依赖了未知单元: {d}")

    # 3. 分层满足拓扑序：任意 unit 的依赖都在更早的层（循环依赖同层豁免）
    unit_layer: dict[str, int] = {}
    for depth, layer in enumerate(layers):
        for u in layer:
            unit_layer[u.name] = depth
    for name, dep_set in unit_deps.items():
        for d in dep_set:
            if name in unit_layer and d in unit_layer:
                if unit_layer[d] > unit_layer[name]:
                    violations.append(
                        f"拓扑序违规: {name}(第{unit_layer[name]}层) 依赖 {d}(第{unit_layer[d]}层)"
                    )

    # 4. 硬绑定组不可拆散
    file_to_unit: dict[str, str] = {}
    for u in units:
        for src in u.sources:
            file_to_unit[norm_path(src)] = u.name
    for group in hard_groups:
        owners = {file_to_unit.get(norm_path(f)) for f in group}
        owners.discard(None)
        if len(owners) > 1:
            violations.append(f"硬绑定组被拆散到 {sorted(owners)}: {group}")

    return violations


# ============================================================
# 产物导出（JSON + mermaid）
# ============================================================

def export_artifacts(
    out_dir,
    units: list,
    file_graph: dict[str, set[str]],
    hard_groups: list[list[str]],
    unit_deps: dict[str, set[str]],
    layers: list[list],
    cycles: list[list[str]],
    violations: list[str],
):
    """把中间产物落盘，便于人工核查与回放"""
    import json
    import os

    out_dir = Path(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    data = {
        "units": [
            {"name": u.name, "sources": [norm_path(s) for s in u.sources],
             "description": u.description}
            for u in units
        ],
        "hard_groups": hard_groups,
        "file_graph": {k: sorted(v) for k, v in file_graph.items()},
        "unit_deps": {k: sorted(v) for k, v in unit_deps.items()},
        "layers": [[u.name for u in layer] for layer in layers],
        "cycles": cycles,
        "violations": violations,
    }
    with open(out_dir / "translation_plan.json", 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # mermaid 依赖图（unit 级）
    lines = ["graph TD"]
    name_to_id = {u.name: f"U{i}" for i, u in enumerate(units)}
    for u in units:
        label = u.name.replace('"', "'")
        lines.append(f'    {name_to_id[u.name]}["{label}"]')
    for name, dep_set in unit_deps.items():
        for d in sorted(dep_set):
            if name in name_to_id and d in name_to_id:
                lines.append(f"    {name_to_id[name]} --> {name_to_id[d]}")
    with open(out_dir / "unit_graph.mmd", 'w', encoding='utf-8') as f:
        f.write("\n".join(lines))

    print(f"  产物已导出: {out_dir / 'translation_plan.json'}, {out_dir / 'unit_graph.mmd'}")

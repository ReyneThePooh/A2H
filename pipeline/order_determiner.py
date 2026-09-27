"""翻译顺序确定器 — unit 划分 + 依赖分析 + 拓扑排序"""

import os
import json
import re
from pathlib import Path
from dataclasses import dataclass, field
from run_control import BudgetExceeded, check_budget

from hello_agents.core.llm import HelloAgentsLLM

from analyzers.static import (
    JavaStaticAnalyzer, XmlStaticAnalyzer,
    FileSummary, XmlSummary,
    MethodSummary, FieldSummary, ParamSummary, ReturnSummary,
    analyze_file,
)
from analyzers.tools import ToolRegistry, _summary_to_dict
from pipeline.static_graph import (
    norm_path, build_file_graph, build_hard_groups,
    layered_topological_sort, tarjan_scc, validate_plan, export_artifacts,
)
from pipeline.agents import (
    create_pipeline_llm, UnitBuildAgent, DependencyReviewAgent,
)
from pipeline.artifacts import ArtifactContractError, atomic_write_json, content_hash


# ============================================================
# 数据模型
# ============================================================

@dataclass
class Unit:
    """翻译单元"""
    name: str
    sources: list[str] = field(default_factory=list)  # 包含的源文件路径
    description: str = ""  # 单元功能描述（LLM 生成）


# ============================================================
# 步骤 1: 文件扫描
# ============================================================

def scan_project(project_path: str, src_dir: str = "app/src/main") -> dict[str, str]:
    """扫描项目，返回 {相对路径: 文件内容}"""
    root = Path(project_path) / src_dir
    if not root.exists():
        # 尝试项目根目录就直接是源码目录
        root = Path(project_path)

    files = {}
    for ext in ['*.java', '*.xml']:
        for f in root.rglob(ext):
            rel = norm_path(f.relative_to(root))
            try:
                files[rel] = f.read_text(encoding='utf-8')
            except (OSError, UnicodeError) as exc:
                raise ArtifactContractError(f"Unreadable source file: {f}") from exc

    # 只保留 .java 和 layout 目录下的 .xml
    filtered = {}
    for path, content in files.items():
        pl = path.lower()
        if path.endswith('.java'):
            filtered[path] = content
        elif path.endswith('.xml') and 'layout' in pl:
            filtered[path] = content

    return filtered


# ============================================================
# 步骤 2: 摘要生成（静态 + LLM）
# ============================================================

class SummaryGenerator:
    """为项目所有文件生成摘要"""

    def __init__(self, llm: HelloAgentsLLM):
        self.llm = llm

    def generate_all(self, project_path: str, files: dict[str, str],
                     cache_path: str | None = None) -> dict[str, FileSummary | XmlSummary]:
        """生成所有文件摘要，优先从缓存加载"""
        summaries = {}

        fingerprint_path = Path(cache_path + ".fingerprints.json") if cache_path else None
        fingerprint_context = {"schema": 2, "model": os.getenv("LLM_MODEL_ID", ""),
                               "analyzer": content_hash(Path(__file__).read_bytes()),
                               "static_analyzer": content_hash((Path(__file__).parent.parent / "analyzers/static.py").read_bytes())}
        fingerprints = {path: content_hash(json.dumps(fingerprint_context, sort_keys=True) + "\n" + code)
                        for path, code in files.items()}
        old_fingerprints = {}
        if fingerprint_path:
            try:
                old_fingerprints = json.loads(fingerprint_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        # Cache only unchanged files; additions/deletions and dependency analysis
        # always see the current source inventory.
        if cache_path and os.path.exists(cache_path):
            cached = self._load_cache(cache_path)
            if cached:
                summaries = {path: summary for path, summary in cached.items()
                             if path in fingerprints and old_fingerprints.get(path) == fingerprints[path]}
        reused = set(summaries)

        # 第一阶段：静态分析（零 token）
        for file_path, code in files.items():
            if file_path in reused:
                continue
            s = analyze_file(file_path, code)
            if s:
                summaries[file_path] = s

        # 第二阶段：LLM 补充语义摘要
        java_files = [(p, s) for p, s in summaries.items() if isinstance(s, FileSummary) and p not in reused]
        xml_files = [(p, s) for p, s in summaries.items() if isinstance(s, XmlSummary) and p not in reused]

        print(f"  静态分析完成: {len(java_files)} Java, {len(xml_files)} XML")
        print(f"  LLM 语义摘要生成中...")

        for i, (file_path, s) in enumerate(java_files):
            check_budget()
            print(f"    [{i + 1}/{len(java_files)}] {Path(file_path).name}")
            self._summarize_java(file_path, s)

        for i, (file_path, s) in enumerate(xml_files):
            check_budget()
            if s.is_empty():
                continue
            print(f"    [{i + 1}/{len(xml_files)}] {Path(file_path).name}")
            self._summarize_xml(file_path, s)

        # 保存缓存
        if cache_path:
            self._save_cache(summaries, cache_path)
            atomic_write_json(fingerprint_path, fingerprints)

        return summaries

    def _summarize_java(self, file_path: str, s: FileSummary):
        """LLM 为 Java 文件生成语义摘要"""
        static_json = json.dumps({
            "class": s.class_name,
            "package": s.package,
            "class_type": s.class_type,
            "extends": s.extends,
            "implements": s.implements,
            "methods": [
                {"name": m.name, "sig": m.sig, "lines": m.lines, "vis": m.vis,
                 "params": [{"name": p.name, "type": p.type} for p in m.params],
                 "return_type": m.returns.type if m.returns else "void"}
                for m in s.methods
            ],
            "fields": [{"name": f.name, "type": f.type, "vis": f.vis} for f in s.fields],
            "features": {
                "has_lifecycle": s.has_lifecycle,
                "has_event_listeners": s.has_event_listeners,
                "has_findViewById": s.has_find_view_by_id,
            },
        }, ensure_ascii=False)

        prompt = f"""请分析以下Java文件的静态信息，生成语义摘要。直接返回JSON，不要markdown包裹。

## 文件静态信息
{static_json}

## 要求
返回如下JSON：
{{
  "class_purpose": "一句话描述这个类的作用（中文，20字以内）",
  "class_role": "这个类的角色，如：页面控制器、数据模型、工具类、数据库操作、适配器",
  "design_pattern": "使用的设计模式，如：Activity+Adapter、Singleton、无",
  "is_stateful": true/false,
  "lifecycle_dependent": true/false,
  "call_flow": "主要方法调用链，如：onCreate→loadData→adapter刷新",
  "methods": [
    {{
      "name": "方法名",
      "purpose": "该方法的作用（20字以内）",
      "params": [{{"name": "参数名", "purpose": "参数含义"}}],
      "returns": {{"meaning": "返回值含义"}},
      "side_effects": ["副作用1"]
    }}
  ],
  "fields": [
    {{"name": "字段名", "purpose": "该字段的业务含义"}}
  ]
}}

只返回JSON。"""

        try:
            result = self.llm.invoke([{"role": "user", "content": prompt}])
            data = self._parse_object(result)
            if not isinstance(data, dict):
                repair_prompt = (
                    f"{prompt}\n\n上一次响应不是可用的 JSON 对象。"
                    "请重新输出一个且仅一个 JSON 对象，不要数组、Markdown、工具参数或额外说明。"
                )
                result = self.llm.invoke([{"role": "user", "content": repair_prompt}])
                data = self._parse_object(result)
            if not isinstance(data, dict):
                raise ValueError("semantic summary is not a JSON object")
            if not isinstance(data.get("methods", []), list) or not isinstance(data.get("fields", []), list):
                raise ValueError("semantic summary methods/fields must be arrays")
            s.class_purpose = data.get("class_purpose", "")
            s.class_role = data.get("class_role", "")
            s.design_pattern = data.get("design_pattern", "")
            s.is_stateful = data.get("is_stateful", False)
            s.lifecycle_dependent = data.get("lifecycle_dependent", False)
            s.call_flow = data.get("call_flow", "")

            # 填充方法语义
            llm_methods = {m["name"]: m for m in data.get("methods", [])}
            for m in s.methods:
                lm = llm_methods.get(m.name, {})
                m.purpose = lm.get("purpose", "")
                for p in m.params:
                    lp_map = {pp["name"]: pp for pp in lm.get("params", [])}
                    p.purpose = lp_map.get(p.name, {}).get("purpose", "")
                if m.returns and lm.get("returns"):
                    m.returns.meaning = lm["returns"].get("meaning", "")
                m.side_effects = lm.get("side_effects", [])

            # 填充字段语义
            llm_fields = {f["name"]: f for f in data.get("fields", [])}
            for f in s.fields:
                lf = llm_fields.get(f.name, {})
                f.purpose = lf.get("purpose", "")
        except BudgetExceeded:
            raise
        except Exception as e:
            raise ArtifactContractError(f"Semantic summary failed for {file_path}: {e}") from e

    def _summarize_xml(self, file_path: str, s: XmlSummary):
        """LLM 为 XML 布局生成语义摘要"""
        static_json = json.dumps({
            "root": s.root_widget,
            "orientation": s.root_orientation,
            "widgets": s.widgets,
            "ids": s.ids,
            "resource_refs": s.resource_refs,
        }, ensure_ascii=False)

        prompt = f"""请分析以下Android布局XML的静态信息，生成语义摘要。直接返回JSON。

## 布局静态信息
{static_json}

## 要求
返回如下JSON：
{{
  "purpose": "这个布局的作用（20字以内）",
  "layout_pattern": "布局模式，如：列表+浮动按钮、表单、垂直列表、简单卡片",
  "hierarchy": "控件层级结构简述",
  "data_binding": ["控件id绑定到的数据"],
  "event_handling": ["控件id对应的事件"]
}}

只返回JSON。"""

        try:
            result = self.llm.invoke([{"role": "user", "content": prompt}])
            data = self._parse_object(result)
            if not isinstance(data, dict):
                repair_prompt = (
                    f"{prompt}\n\n上一次响应不是可用的 JSON 对象。"
                    "请重新输出一个且仅一个 JSON 对象，不要数组、Markdown、工具参数或额外说明。"
                )
                result = self.llm.invoke([{"role": "user", "content": repair_prompt}])
                data = self._parse_object(result)
            if not isinstance(data, dict):
                raise ValueError("layout summary is not a JSON object")
            s.purpose = data.get("purpose", "")
            s.layout_pattern = data.get("layout_pattern", "")
            s.hierarchy = data.get("hierarchy", "")
            s.data_binding = data.get("data_binding", [])
            s.event_handling = data.get("event_handling", [])
        except BudgetExceeded:
            raise
        except Exception as e:
            raise ArtifactContractError(f"Semantic summary failed for {file_path}: {e}") from e

    @staticmethod
    def _refresh_static_fields(summaries: dict, files: dict[str, str]) -> bool:
        """为旧版缓存补齐新增的静态字段（type_references / intent_targets），零 token"""
        refreshed = False
        for path, s in summaries.items():
            if not isinstance(s, FileSummary):
                continue
            if s.type_references or s.intent_targets:
                continue
            code = files.get(path) or files.get(norm_path(path))
            if not code:
                continue
            fresh = JavaStaticAnalyzer.analyze(path, code)
            s.type_references = fresh.type_references
            s.intent_targets = fresh.intent_targets
            if not s.resource_refs:
                s.resource_refs = fresh.resource_refs
            refreshed = True
        if refreshed:
            print("  已为缓存摘要补齐静态依赖字段")
        return refreshed

    @staticmethod
    def _parse_json(text: str) -> dict | None:
        """从 LLM 输出中解析 JSON"""
        text = text.strip()
        # 去掉 markdown 代码块
        if "```" in text:
            parts = text.split("```")
            for i, part in enumerate(parts):
                if i % 2 == 1 and part.strip():
                    inner = part.strip()
                    if inner.startswith(("json", "JSON")):
                        inner = inner[4:].strip()
                    text = inner
                    break
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # 找最外层匹配的 { }
            start = text.find('{')
            count = 0
            end = -1
            for i in range(start, len(text)):
                if text[i] == '{': count += 1
                elif text[i] == '}':
                    count -= 1
                    if count == 0:
                        end = i + 1
                        break
            if start >= 0 and end > start:
                try:
                    return json.loads(text[start:end])
                except json.JSONDecodeError:
                    pass
            return None

    @staticmethod
    def _parse_object(text: str) -> dict | None:
        """Parse one summary object while tolerating known gateway wrappers."""
        if not isinstance(text, str) or not text.strip():
            return None

        payload = text.strip()
        fenced = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", payload, re.I)
        if fenced:
            payload = fenced.group(1).strip()

        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            data = None
        if isinstance(data, dict):
            return data
        if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
            return data[0]

        decoder = json.JSONDecoder()
        values = []
        offset = payload.find("{")
        if offset < 0:
            offset = payload.find("[")
        if offset < 0:
            return None
        while offset < len(payload):
            while offset < len(payload) and payload[offset].isspace():
                offset += 1
            if offset >= len(payload):
                break
            try:
                value, end = decoder.raw_decode(payload, offset)
            except (json.JSONDecodeError, TypeError):
                return None
            values.append(value)
            offset = end

        if len(values) == 1 and isinstance(values[0], dict):
            return values[0]
        if len(values) >= 2 and isinstance(values[-1], dict):
            prefixes = values[:-1]
            if all(SummaryGenerator._is_read_tool_args(value) for value in prefixes):
                return values[-1]
        if len(values) == 1 and isinstance(values[0], list) and len(values[0]) == 1 \
                and isinstance(values[0][0], dict):
            return values[0][0]
        return None

    @staticmethod
    def _is_read_tool_args(value: object) -> bool:
        if not isinstance(value, dict):
            return False
        keys = set(value)
        if keys == {"file_path"}:
            return isinstance(value.get("file_path"), str)
        if keys == {"file_path", "start_line", "end_line"}:
            return (isinstance(value.get("file_path"), str)
                    and isinstance(value.get("start_line"), int)
                    and isinstance(value.get("end_line"), int))
        return not keys

    def _save_cache(self, summaries: dict, cache_path: str):
        """保存摘要缓存"""
        def _to_dict(obj):
            """递归转换 dataclass / 普通对象为可 JSON 序列化的 dict"""
            if isinstance(obj, dict):
                return {k: _to_dict(v) for k, v in obj.items()}
            elif isinstance(obj, (list, tuple)):
                return [_to_dict(v) for v in obj]
            elif hasattr(obj, '__dict__'):
                result = {}
                for k, v in obj.__dict__.items():
                    result[k] = _to_dict(v)
                return result
            elif hasattr(obj, '__dataclass_fields__'):
                result = {}
                for f_name in obj.__dataclass_fields__:
                    result[f_name] = _to_dict(getattr(obj, f_name))
                return result
            else:
                return obj

        data = {}
        for path, s in summaries.items():
            if isinstance(s, FileSummary):
                data[path] = {"type": "java", "data": _to_dict(s)}
            elif isinstance(s, XmlSummary):
                data[path] = {"type": "xml", "data": _to_dict(s)}

        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        atomic_write_json(Path(cache_path), data)
        print(f"  摘要缓存已保存: {cache_path}")

    def _load_cache(self, cache_path: str) -> dict | None:
        """加载摘要缓存"""
        def _restore_methods(methods_data: list) -> list[MethodSummary]:
            result = []
            for m in methods_data if methods_data else []:
                params = [ParamSummary(**p) for p in m.get("params", [])]
                returns = ReturnSummary(**m["returns"]) if m.get("returns") else None
                result.append(MethodSummary(
                    name=m.get("name", ""), sig=m.get("sig", ""),
                    start_line=m.get("start_line", 0), end_line=m.get("end_line", 0),
                    lines=m.get("lines", 0), vis=m.get("vis", ""),
                    purpose=m.get("purpose", ""), params=params,
                    returns=returns, side_effects=m.get("side_effects", []),
                ))
            return result

        def _restore_fields(fields_data: list) -> list[FieldSummary]:
            if not fields_data:
                return []
            return [FieldSummary(**f) for f in fields_data]

        try:
            with open(cache_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            summaries = {}
            for path, entry in data.items():
                d = entry["data"]
                if entry["type"] == "java":
                    s = FileSummary(file_path=path)
                    s.class_name = d.get("class_name", "")
                    s.package = d.get("package", "")
                    s.class_type = d.get("class_type", "")
                    s.extends = d.get("extends", "")
                    s.implements = d.get("implements", [])
                    s.class_line = d.get("class_line", 0)
                    s.project_imports = d.get("project_imports", [])
                    s.import_start = d.get("import_start", 0)
                    s.import_end = d.get("import_end", 0)
                    s.android_imports_count = d.get("android_imports_count", 0)
                    s.type_references = d.get("type_references", [])
                    s.intent_targets = d.get("intent_targets", [])
                    s.resource_refs = d.get("resource_refs", {})
                    s.lines = d.get("lines", 0)
                    s.methods = _restore_methods(d.get("methods", []))
                    s.fields = _restore_fields(d.get("fields", []))
                    s.has_lifecycle = d.get("has_lifecycle", False)
                    s.has_event_listeners = d.get("has_event_listeners", False)
                    s.has_find_view_by_id = d.get("has_find_view_by_id", False)
                    s.class_purpose = d.get("class_purpose", "")
                    s.class_role = d.get("class_role", "")
                    s.design_pattern = d.get("design_pattern", "")
                    s.is_stateful = d.get("is_stateful", False)
                    s.lifecycle_dependent = d.get("lifecycle_dependent", False)
                    s.call_flow = d.get("call_flow", "")
                    summaries[path] = s
                elif entry["type"] == "xml":
                    s = XmlSummary(file_path=path)
                    s.xml_type = d.get("xml_type", "")
                    s.root_widget = d.get("root_widget", "")
                    s.root_orientation = d.get("root_orientation", "")
                    s.widgets = d.get("widgets", [])
                    s.ids = d.get("ids", [])
                    s.resource_refs = d.get("resource_refs", {})
                    s.lines = d.get("lines", 0)
                    s.purpose = d.get("purpose", "")
                    s.layout_pattern = d.get("layout_pattern", "")
                    s.hierarchy = d.get("hierarchy", "")
                    s.data_binding = d.get("data_binding", [])
                    s.event_handling = d.get("event_handling", [])
                    summaries[path] = s

            print(f"  从缓存加载 {len(summaries)} 个摘要")
            return summaries
        except Exception as e:
            print(f"  缓存加载失败: {e}")
            return None


# ============================================================
# 步骤 3: Unit 划分（LLM）
# ============================================================

class UnitBuilder:
    """将文件分组为翻译单元 — LLM 部分委托给 UnitBuildAgent"""

    def __init__(self, llm: HelloAgentsLLM, tools: ToolRegistry):
        self.llm = llm
        self.tools = tools

    def build_units(self, java_files: list[str], xml_files: list[str],
                    hard_groups: list[list[str]] | None = None) -> list[Unit]:
        """LLM 驱动的 unit 划分（受静态硬绑定约束）"""
        hard_groups = hard_groups or []
        hard_groups_text = "\n".join(
            f"- 组{i + 1}: {', '.join(g)}" for i, g in enumerate(hard_groups)
        ) or "（无）"

        prompt = f"""请为以下项目划分翻译单元。

## Java 文件 ({len(java_files)} 个)
{chr(10).join(java_files)}

## XML 布局文件 ({len(xml_files)} 个)
{chr(10).join(xml_files)}

## 硬绑定组（组内文件必须同单元）
{hard_groups_text}

请先使用 get_all_summaries 了解全貌，然后对关键文件使用 get_file_summary 查看详情，最后输出单元划分。"""

        agent = UnitBuildAgent(self.llm, self.tools)
        result = agent.run(prompt)

        data = SummaryGenerator._parse_json(result)
        if not data:
            print("  ⚠️ LLM unit 划分解析失败，使用硬绑定分组兜底")
            return self._fallback_units(java_files, xml_files, hard_groups)

        all_files = [norm_path(f) for f in java_files + xml_files]
        units = []
        for u in data.get("units", []):
            sources = self._normalize_sources(u.get("sources", []), all_files)
            if sources:
                units.append(Unit(
                    name=u.get("name", "未命名"),
                    sources=sources,
                    description=u.get("description", ""),
                ))

        units = self._repair_units(units, all_files, hard_groups)
        return units

    @staticmethod
    def _normalize_sources(sources: list[str], all_files: list[str]) -> list[str]:
        """把 LLM 输出的路径规范化并匹配到真实文件；无法匹配的丢弃并告警"""
        result = []
        for src in sources:
            src_n = norm_path(src)
            if src_n in all_files:
                result.append(src_n)
                continue
            # 结尾匹配（LLM 可能省略前缀目录）
            matches = [f for f in all_files if f.endswith('/' + src_n) or f == src_n]
            if len(matches) == 1:
                result.append(matches[0])
            else:
                print(f"  ⚠️ LLM 输出了无法匹配的路径，已丢弃: {src}")
        return result

    def _repair_units(self, units: list[Unit], all_files: list[str],
                      hard_groups: list[list[str]]) -> list[Unit]:
        """修复 LLM 输出：去重、补漏、强制执行硬绑定约束"""
        # 1. 去重：一个文件只保留在第一个出现的单元中
        seen: set[str] = set()
        for u in units:
            deduped = []
            for src in u.sources:
                if src not in seen:
                    seen.add(src)
                    deduped.append(src)
                else:
                    print(f"  ⚠️ 文件被重复分配，保留首个单元: {src}")
            u.sources = deduped

        # 2. 补漏：未覆盖的文件创建单独单元
        missing = set(all_files) - seen
        if missing:
            print(f"  ⚠️ LLM 遗漏 {len(missing)} 个文件，为它们创建单独单元")
            for f in sorted(missing):
                units.append(Unit(name=Path(f).stem, sources=[f], description="自动补充"))

        # 3. 硬绑定：组内文件若分散在多个单元，全部移入锚点单元（组内第一个 Java 文件所在单元）
        file_to_unit: dict[str, Unit] = {}
        for u in units:
            for src in u.sources:
                file_to_unit[src] = u

        for group in hard_groups:
            group_n = [norm_path(f) for f in group if norm_path(f) in file_to_unit]
            if not group_n:
                continue
            owners = {id(file_to_unit[f]) for f in group_n}
            if len(owners) <= 1:
                continue
            anchor_file = next((f for f in group_n if f.endswith('.java')), group_n[0])
            anchor = file_to_unit[anchor_file]
            print(f"  ⚠️ 硬绑定组被拆散，自动合并到单元 [{anchor.name}]: {group_n}")
            for f in group_n:
                current = file_to_unit[f]
                if current is not anchor:
                    current.sources.remove(f)
                    anchor.sources.append(f)
                    file_to_unit[f] = anchor

        # 4. 清理空单元
        return [u for u in units if u.sources]

    def _fallback_units(self, java_files: list[str], xml_files: list[str],
                        hard_groups: list[list[str]] | None = None) -> list[Unit]:
        """兜底：按硬绑定分组成单元，剩余文件每个一个单元"""
        hard_groups = hard_groups or []
        all_files = [norm_path(f) for f in java_files + xml_files]
        units = []
        grouped: set[str] = set()

        for g in hard_groups:
            g_n = [norm_path(f) for f in g if norm_path(f) in all_files]
            if not g_n:
                continue
            anchor = next((f for f in g_n if f.endswith('.java')), g_n[0])
            units.append(Unit(name=Path(anchor).stem, sources=g_n))
            grouped.update(g_n)

        for f in all_files:
            if f not in grouped:
                units.append(Unit(name=Path(f).stem, sources=[f]))
        return units


# ============================================================
# 步骤 4: Unit 依赖分析（静态文件图 + LLM 审核补充）
# ============================================================

class UnitDependencyAnalyzer:
    """确定 unit 之间的依赖关系

    静态第一遍：把文件级依赖图（类型引用 / import / Intent / 布局引用）投影到 unit 级，
    覆盖同包无 import 的引用；LLM 第二遍（DependencyReviewAgent）只负责审核和补充
    静态无法确定的隐式依赖。
    """

    def __init__(self, llm: HelloAgentsLLM, tools: ToolRegistry, summaries: dict,
                 file_graph: dict[str, set[str]] | None = None):
        self.llm = llm
        self.tools = tools
        self.summaries = summaries
        self.file_graph = file_graph if file_graph is not None else build_file_graph(summaries)

    def analyze(self, units: list[Unit], use_llm: bool = True) -> dict[str, set[str]]:
        """分析 unit 依赖关系 — 静态第一遍，LLM 第二遍（可关闭）"""
        name_to_unit = {u.name: u for u in units}
        file_to_unit: dict[str, str] = {}
        for u in units:
            for src in u.sources:
                file_to_unit[norm_path(src)] = u.name

        # ---- 静态第一遍：文件级依赖图投影到 unit 级 ----
        static_deps: dict[str, set[str]] = {u.name: set() for u in units}
        for file_path, dep_files in self.file_graph.items():
            src_unit = file_to_unit.get(norm_path(file_path))
            if not src_unit:
                continue
            for dep_file in dep_files:
                dep_unit = file_to_unit.get(norm_path(dep_file))
                if dep_unit and dep_unit != src_unit:
                    static_deps[src_unit].add(dep_unit)

        print(f"  静态依赖结果:")
        for name in sorted(static_deps.keys()):
            deps = static_deps[name]
            print(f"    {name}: 依赖 {sorted(deps) if deps else '无'}")

        if not use_llm:
            return static_deps

        # ---- LLM 第二遍：审核并补充隐式依赖 ----
        unit_descriptions = []
        for u in units:
            sources_desc = []
            for src in u.sources:
                s = self.summaries.get(src) or self.summaries.get(norm_path(src))
                if isinstance(s, FileSummary):
                    sources_desc.append(
                        f"  [{Path(src).name}] {s.class_name} ({s.class_type}) — "
                        f"extends={s.extends}, purpose={s.class_purpose}, call_flow={s.call_flow}"
                    )
                elif isinstance(s, XmlSummary):
                    sources_desc.append(
                        f"  [{Path(src).name}] layout root={s.root_widget} — {s.purpose}"
                    )
            unit_descriptions.append(
                f"## {u.name}\n" + "\n".join(sources_desc)
            )

        static_deps_json = json.dumps(
            {k: sorted(v) for k, v in static_deps.items()}, ensure_ascii=False, indent=2
        )

        prompt = f"""## 单元详情
{chr(10).join(unit_descriptions)}

## 单元列表
{json.dumps([u.name for u in units], ensure_ascii=False)}

## 静态分析已得到的依赖（键依赖值列表，请保留并在此基础上补充）
{static_deps_json}

## 分析重点
1. 静态依赖已覆盖 import、同包类型引用、Intent 跳转、布局引用，无需重复检查
2. 重点找隐式协议依赖：共享的持久化数据、广播、全局状态
3. 根据摘要直接分析即可，仅在摘要不足时调用工具

请输出依赖关系 JSON。"""

        try:
            agent = DependencyReviewAgent(self.llm, self.tools)
            final = agent.run(prompt)

            print(f"  LLM 原始输出: {final[:300]}...")

            data = SummaryGenerator._parse_json(final)
            if data and "dependencies" in data:
                llm_deps = data["dependencies"]
                added = []
                for unit_name, dep_list in llm_deps.items():
                    if unit_name in static_deps:
                        for dep in dep_list:
                            if dep in name_to_unit and dep != unit_name \
                                    and dep not in static_deps[unit_name]:
                                static_deps[unit_name].add(dep)
                                added.append(f"{unit_name} → {dep}")
                if added:
                    print(f"  LLM 补充依赖: {added}")

                reasoning = data.get("reasoning", "")
                if reasoning:
                    print(f"  LLM 分析: {reasoning}")
            else:
                print(f"  ⚠️ LLM 输出 JSON 解析失败，保留静态依赖结果")
        except BudgetExceeded:
            raise
        except Exception as e:
            print(f"  ⚠️ LLM 依赖分析失败: {e}，保留静态依赖结果")

        return static_deps


# ============================================================
# 步骤 5: 拓扑排序
# ============================================================

def merge_cyclic_units(units: list[Unit], deps: dict[str, set[str]]) -> tuple[list[Unit], dict[str, set[str]]]:
    """Collapse mutually dependent units into one translation task, retaining all sources."""
    by_name = {unit.name: unit for unit in units}
    if len(by_name) != len(units):
        raise ArtifactContractError("Translation unit names must be unique")
    for name, targets in deps.items():
        if name not in by_name or not targets.issubset(by_name):
            raise ArtifactContractError(f"Unknown translation dependencies: {name} -> {sorted(targets)}")
    groups, owner = [], {}
    for members in tarjan_scc(list(by_name), deps):
        if len(members) == 1:
            group = by_name[members[0]]
        else:
            name = "联合翻译[" + "、".join(members) + "]"
            if name in by_name:
                raise ArtifactContractError(f"Translation group name collision: {name}")
            group = Unit(name,
                         sorted({source for member in members for source in by_name[member].sources}),
                         "\n".join(f"{member}: {by_name[member].description}" for member in members))
            print(f"  循环依赖合并翻译: {members}")
        groups.append(group)
        owner.update({member: group.name for member in members})
    grouped_deps = {group.name: set() for group in groups}
    for name, targets in deps.items():
        grouped_deps[owner[name]].update(owner[target] for target in targets if owner[target] != owner[name])
    return groups, grouped_deps


def topological_sort(
    units: list[Unit], deps: dict[str, set[str]]
) -> tuple[list[list[Unit]], list[list[str]]]:
    """将 unit 依赖图拓扑排序（Tarjan SCC 缩点 + 最长路径分层）。

    返回 (layers, cycles)：
    - layers: 分层列表，任意 unit 的所有依赖都在更早的层；同层可并行翻译
    - cycles: 循环依赖分组（同一 SCC 的 unit 名列表），环内成员被放在同一层，
      翻译时应把环内所有成员的摘要一起放进上下文
    """
    name_to_unit = {u.name: u for u in units}
    names = [u.name for u in units]

    name_layers, cycles = layered_topological_sort(names, deps)

    if cycles:
        for cycle in cycles:
            print(f"  ⚠️ 检测到循环依赖，成员将放入同一层: {cycle}")

    layers: list[list[Unit]] = []
    for layer_names in name_layers:
        layer = [name_to_unit[n] for n in layer_names if n in name_to_unit]
        # 层内排序：被依赖多的排前面，串行执行时先翻译更基础的 unit
        dependents_count = {
            n: sum(1 for dep_set in deps.values() if n in dep_set) for n in layer_names
        }
        layer.sort(key=lambda u: (-dependents_count.get(u.name, 0), u.name))
        if layer:
            layers.append(layer)

    return layers, cycles


# ============================================================
# 主编排器
# ============================================================

class OrderDeterminer:
    """翻译顺序确定 — 编排整个流程"""

    def __init__(self, project_path: str, cache_dir: str = ".pipeline_cache"):
        self.project_path = Path(project_path)
        self.cache_dir = self.project_path / cache_dir
        self.llm = create_pipeline_llm()
        self.summaries: dict[str, FileSummary | XmlSummary] = {}

    def run(self) -> tuple[list[list[Unit]], dict[str, set[str]]]:
        """执行完整流程，返回分层 unit 列表和依赖关系"""
        print("=" * 60)
        print(f"翻译顺序确定 — 项目: {self.project_path}")
        print("=" * 60)

        # 1. 扫描
        print("\n[1/5] 扫描项目...")
        files = scan_project(str(self.project_path))
        java_files = sorted([p for p in files if p.endswith('.java')])
        xml_files = sorted([p for p in files if p.endswith('.xml')])
        print(f"  Java: {len(java_files)} 个, XML: {len(xml_files)} 个")

        # 2. 生成摘要
        print(f"\n[2/5] 生成文件摘要...")
        cache_path = str(self.cache_dir / "summaries.json")
        generator = SummaryGenerator(self.llm)
        self.summaries = generator.generate_all(
            str(self.project_path), files, cache_path=cache_path
        )

        # 3. 静态依赖层：文件级依赖图 + Java↔XML 硬绑定分组
        print(f"\n[3/6] 构建静态依赖图与硬绑定分组...")
        file_graph = build_file_graph(self.summaries)
        hard_groups = build_hard_groups(self.summaries)
        edge_count = sum(len(v) for v in file_graph.values())
        print(f"  文件级依赖边: {edge_count} 条, 硬绑定组: {len(hard_groups)} 个")
        for g in hard_groups:
            print(f"    组: {[Path(f).name for f in g]}")

        # 4. 划分 Unit（受硬绑定约束）
        print(f"\n[4/6] LLM 划分翻译单元...")
        tools = ToolRegistry(str(self.project_path), self.summaries)
        unit_builder = UnitBuilder(self.llm, tools)
        units = unit_builder.build_units(java_files, xml_files, hard_groups)

        print(f"  划分结果: {len(units)} 个单元")
        for u in units:
            names = [Path(s).name for s in u.sources]
            print(f"    [{u.name}] → {names}")

        # 5. 分析 Unit 依赖（静态投影 + LLM 补充）
        print(f"\n[5/6] 分析 Unit 依赖...")
        dep_analyzer = UnitDependencyAnalyzer(
            self.llm, tools, self.summaries, file_graph=file_graph
        )
        unit_deps = dep_analyzer.analyze(units)

        # 6. 拓扑排序（SCC 缩点）
        print(f"\n[6/6] 拓扑排序...")
        layers, cycles = topological_sort(units, unit_deps)

        print(f"\n翻译顺序（分 {len(layers)} 层）:")
        for depth, layer in enumerate(layers):
            names = [u.name for u in layer]
            deps_info = ", ".join(
                f"→ {d}" for u in layer for d in sorted(unit_deps.get(u.name, set()))
            )[:100]
            print(f"  第{depth}层: {names}  {deps_info}")

        # 校验不变量并导出产物
        all_files = java_files + xml_files
        violations = validate_plan(units, unit_deps, layers, all_files, hard_groups)
        if violations:
            print(f"\n⚠️ 校验发现 {len(violations)} 个问题:")
            for v in violations:
                print(f"  - {v}")
        else:
            print(f"\n✓ 校验通过：文件覆盖 / 依赖边 / 拓扑序 / 硬绑定均满足")

        export_artifacts(
            self.cache_dir, units, file_graph, hard_groups,
            unit_deps, layers, cycles, violations,
        )

        if violations:
            raise ArtifactContractError("Translation plan validation failed: " + "; ".join(map(str, violations)))

        return layers, unit_deps


# ============================================================
# 测试入口
# ============================================================

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    # 测试 sample 项目
    project = "/Users/kxh/Desktop/横向/sample-android-project"
    determiner = OrderDeterminer(project)
    layers, deps = determiner.run()

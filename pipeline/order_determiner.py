"""翻译顺序确定器 — unit 划分 + 依赖分析 + 拓扑排序"""

import os
import json
import time
from pathlib import Path
from dataclasses import dataclass, field
from collections import deque, defaultdict

from openai import OpenAI

from analyzers.static import (
    JavaStaticAnalyzer, XmlStaticAnalyzer,
    FileSummary, XmlSummary,
    MethodSummary, FieldSummary, ParamSummary, ReturnSummary,
    analyze_file,
)
from analyzers.tools import ToolRegistry, _summary_to_dict


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
# LLM 客户端封装
# ============================================================

class LLMClient:
    """轻量 LLM 封装 — 直接使用 OpenAI API"""

    def __init__(self):
        self.client = OpenAI(
            api_key=os.getenv("LLM_API_KEY"),
            base_url=os.getenv("LLM_BASE_URL"),
            timeout=int(os.getenv("LLM_TIMEOUT", "120")),
        )
        self.model = os.getenv("LLM_MODEL_ID", "gpt-3.5-turbo")

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             tool_choice: str = "auto") -> dict:
        """发送请求并返回响应消息"""
        kwargs = dict(
            model=self.model,
            messages=messages,
            temperature=0.3,
        )
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice

        response = self.client.chat.completions.create(**kwargs)
        msg = response.choices[0].message
        return {
            "content": msg.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                }
                for tc in (msg.tool_calls or [])
            ],
        }

    def chat_with_tools(self, messages: list[dict], tools: list[dict],
                        tool_executor, max_rounds: int = 6) -> str:
        """带工具调用的对话循环，返回最终文本"""
        for _ in range(max_rounds):
            result = self.chat(messages, tools=tools)

            if result["tool_calls"]:
                # 把助手消息加入历史
                assistant_msg = {"role": "assistant", "content": result["content"]}
                if result["tool_calls"]:
                    tool_call_blocks = []
                    for tc in result["tool_calls"]:
                        tool_call_blocks.append({
                            "id": tc["id"],
                            "type": "function",
                            "function": {"name": tc["name"], "arguments": tc["arguments"]},
                        })
                    assistant_msg["tool_calls"] = tool_call_blocks
                messages.append(assistant_msg)

                # 执行工具调用
                for tc in result["tool_calls"]:
                    tool_result = tool_executor(tc["name"], json.loads(tc["arguments"]))
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": tool_result,
                    })
            else:
                return result["content"]

        return result["content"]


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
            rel = str(f.relative_to(root))
            try:
                files[rel] = f.read_text(encoding='utf-8')
            except Exception:
                pass

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

    def __init__(self, llm: LLMClient):
        self.llm = llm

    def generate_all(self, project_path: str, files: dict[str, str],
                     cache_path: str | None = None) -> dict[str, FileSummary | XmlSummary]:
        """生成所有文件摘要，优先从缓存加载"""
        summaries = {}

        # 尝试加载缓存
        if cache_path and os.path.exists(cache_path):
            cached = self._load_cache(cache_path)
            if cached:
                return cached

        # 第一阶段：静态分析（零 token）
        for file_path, code in files.items():
            s = analyze_file(file_path, code)
            if s:
                summaries[file_path] = s

        # 第二阶段：LLM 补充语义摘要
        java_files = [(p, s) for p, s in summaries.items() if isinstance(s, FileSummary)]
        xml_files = [(p, s) for p, s in summaries.items() if isinstance(s, XmlSummary)]

        print(f"  静态分析完成: {len(java_files)} Java, {len(xml_files)} XML")
        print(f"  LLM 语义摘要生成中...")

        for i, (file_path, s) in enumerate(java_files):
            print(f"    [{i + 1}/{len(java_files)}] {Path(file_path).name}")
            self._summarize_java(file_path, s)

        for i, (file_path, s) in enumerate(xml_files):
            if s.is_empty():
                continue
            print(f"    [{i + 1}/{len(xml_files)}] {Path(file_path).name}")
            self._summarize_xml(file_path, s)

        # 保存缓存
        if cache_path:
            self._save_cache(summaries, cache_path)

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
            result = self.llm.chat([{"role": "user", "content": prompt}])
            data = self._parse_json(result["content"])
            if data:
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
        except Exception as e:
            print(f"      ⚠️ LLM 摘要生成失败: {e}")

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
            result = self.llm.chat([{"role": "user", "content": prompt}])
            data = self._parse_json(result["content"])
            if data:
                s.purpose = data.get("purpose", "")
                s.layout_pattern = data.get("layout_pattern", "")
                s.hierarchy = data.get("hierarchy", "")
                s.data_binding = data.get("data_binding", [])
                s.event_handling = data.get("event_handling", [])
        except Exception as e:
            print(f"      ⚠️ LLM 摘要生成失败: {e}")

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
        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
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
    """LLM Agent — 将文件分组为翻译单元"""

    UNIT_BUILDER_PROMPT = """你是一个Android项目架构分析专家。你的任务是将给定的文件列表分组为"翻译单元(Unit)"。

## 什么是翻译单元
一个翻译单元是一组应该一起翻译的源文件，翻译后会生成一个 .ets 文件。合理的单元划分应该：
- 功能内聚：完成同一功能的 Java 和 XML 放在一起
- 大小适中：每单元 1-5 个文件为宜
- 一个 Activity/Fragment + 其布局 XML + 其 Adapter → 一个单元
- 独立的工具类、数据模型 → 各自一个单元

## 可用工具
- get_all_summaries: 列出所有文件
- get_file_summary: 查看文件详细摘要
- read_file_region: 读取文件指定行（仅在摘要不够时使用）

## 任务
根据项目的文件列表和摘要，将文件分组为翻译单元。

## 输出格式
请严格按照以下JSON格式输出（不要markdown包裹）：
{
  "units": [
    {
      "name": "单元名称（中文，简短描述功能）",
      "sources": ["相对路径1.java", "相对路径2.xml"],
      "description": "单元功能简述"
    }
  ],
  "reasoning": "划分思路简述"
}

注意：
1. sources 必须是原始文件路径，与 get_all_summaries 中列出的路径一致
2. 每个文件必须且只能属于一个单元
3. 确保所有文件都被分配到某个单元中"""

    def __init__(self, llm: LLMClient, tools: ToolRegistry):
        self.llm = llm
        self.tools = tools

    def build_units(self, java_files: list[str], xml_files: list[str]) -> list[Unit]:
        """LLM 驱动的 unit 划分"""
        messages = [
            {"role": "system", "content": self.UNIT_BUILDER_PROMPT},
            {"role": "user", "content": f"""请为以下项目划分翻译单元。

## Java 文件 ({len(java_files)} 个)
{chr(10).join(java_files)}

## XML 布局文件 ({len(xml_files)} 个)
{chr(10).join(xml_files)}

请先使用 get_all_summaries 了解全貌，然后对关键文件使用 get_file_summary 查看详情，最后输出单元划分。"""
             },
        ]

        result = self.llm.chat_with_tools(
            messages,
            tools=self.tools.get_tool_schemas(),
            tool_executor=lambda name, args: self.tools.execute(name, args),
        )

        data = SummaryGenerator._parse_json(result)
        if not data:
            print("  ⚠️ LLM unit 划分解析失败，使用默认单文件单元")
            return self._fallback_units(java_files, xml_files)

        units = []
        for u in data.get("units", []):
            units.append(Unit(
                name=u.get("name", "未命名"),
                sources=u.get("sources", []),
                description=u.get("description", ""),
            ))

        # 验证：检查文件覆盖
        covered = set()
        for u in units:
            covered.update(u.sources)
        all_files = set(java_files + xml_files)
        missing = all_files - covered
        if missing:
            print(f"  ⚠️ LLM 遗漏 {len(missing)} 个文件，为它们创建单独单元")
            for f in sorted(missing):
                units.append(Unit(
                    name=Path(f).stem,
                    sources=[f],
                    description="自动补充"
                ))

        return units

    def _fallback_units(self, java_files: list[str], xml_files: list[str]) -> list[Unit]:
        """兜底：每个文件一个单元"""
        units = []
        for f in java_files:
            units.append(Unit(name=Path(f).stem, sources=[f]))
        for f in xml_files:
            units.append(Unit(name=Path(f).stem, sources=[f]))
        return units


# ============================================================
# 步骤 4: Unit 依赖分析（静态匹配 + LLM）
# ============================================================

class UnitDependencyAnalyzer:
    """确定 unit 之间的依赖关系"""

    DEPENDENCY_PROMPT = """你是一个Java项目依赖分析专家。你的任务是确定翻译单元之间的依赖关系。

## 背景
已经通过静态分析完成了初步的依赖匹配（基于 import 语句）。但以下情况静态分析可能遗漏：
1. 运行时通过 Intent 跳转到其他 Activity
2. 通过反射或动态加载
3. 隐式依赖（共享数据模型、全局状态）

## 可用工具
- get_file_summary: 查看文件详细摘要
- read_file_region: 读取文件指定行

## 任务
请确认/修正/补充单元间的依赖关系。

## 输出格式
严格按以下JSON格式输出：
{
  "dependencies": {
    "单元A": ["单元B", "单元C"],
    "单元B": ["单元C"]
  },
  "reasoning": "依赖分析说明"
}

注意：
- 键是被依赖的单元名（depends_on）
- 值列表中的单元名必须与输入中给定的完全一致
- 只输出确实存在的依赖"""

    def __init__(self, llm: LLMClient, tools: ToolRegistry, summaries: dict):
        self.llm = llm
        self.tools = tools
        self.summaries = summaries

    def analyze(self, units: list[Unit]) -> dict[str, set[str]]:
        """分析 unit 依赖关系 — 静态第一遍，LLM 第二遍"""
        # 构建名称索引
        name_to_unit = {u.name: u for u in units}
        file_to_unit = {}
        for u in units:
            for src in u.sources:
                file_to_unit[src] = u.name

        # ---- 静态第一遍：基于 project_imports 匹配 ----
        static_deps: dict[str, set[str]] = {u.name: set() for u in units}

        for u in units:
            for src in u.sources:
                s = self.summaries.get(src)
                if not isinstance(s, FileSummary):
                    continue

                # 检查每个 project_import 属于哪个 unit
                for imp in s.project_imports:
                    dep_unit = self._match_file_to_unit(imp, file_to_unit)
                    if dep_unit and dep_unit != u.name:
                        static_deps[u.name].add(dep_unit)

        print(f"  静态匹配结果:")
        for name in sorted(static_deps.keys()):
            deps = static_deps[name]
            if deps:
                print(f"    {name}: 依赖 {deps}")
            else:
                print(f"    {name}: 无（同包引用，需 LLM 补充）")

        # ---- LLM 第二遍：补充隐式依赖 ----
        unit_descriptions = []
        for u in units:
            sources_desc = []
            for src in u.sources:
                s = self.summaries.get(src)
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

        prompt = f"""## 单元详情
{chr(10).join(unit_descriptions)}

## 单元列表
{json.dumps([u.name for u in units], ensure_ascii=False)}

## 分析重点
1. 所有 Java 文件在同一个 package 下，无需 import 即可引用彼此
2. Activity 之间通过 Intent 跳转（如 MainActivity → HomeActivity）
3. DbHelper 数据库工具类被哪些 Activity 实例化使用
4. 不需要 LLM 调用任何工具，根据摘要直接分析即可

请输出依赖关系 JSON。"""

        try:
            messages = [
                {"role": "system", "content": self.DEPENDENCY_PROMPT},
                {"role": "user", "content": prompt},
            ]
            final = self.llm.chat_with_tools(
                messages,
                tools=self.tools.get_tool_schemas(),
                tool_executor=lambda name, args: self.tools.execute(name, args),
            )

            print(f"  LLM 原始输出: {final[:300]}...")

            data = SummaryGenerator._parse_json(final)
            if data and "dependencies" in data:
                llm_deps = data["dependencies"]
                for unit_name, dep_list in llm_deps.items():
                    if unit_name in static_deps:
                        for dep in dep_list:
                            if dep in name_to_unit:
                                static_deps[unit_name].add(dep)

                reasoning = data.get("reasoning", "")
                if reasoning:
                    print(f"  LLM 分析: {reasoning}")
            else:
                print(f"  ⚠️ LLM 输出 JSON 解析失败")
        except Exception as e:
            print(f"  ⚠️ LLM 依赖分析失败: {e}")
            import traceback
            traceback.print_exc()

        return static_deps

    def _import_to_path(self, imp: str) -> str | None:
        """将 Java import 转为可能的文件路径尾部"""
        # com.example.crudapp.DbHelper → DbHelper.java
        parts = imp.split('.')
        if len(parts) >= 2:
            class_name = parts[-1]
            return f"{class_name}.java"
        return None

    def _match_file_to_unit(self, import_class_name: str, file_to_unit: dict[str, str]) -> str | None:
        """根据 import 的类名在 file_to_unit 中查找匹配的 unit"""
        # com.example.crudapp.DbHelper → DbHelper.java
        parts = import_class_name.split('.')
        target_file = f"{parts[-1]}.java"

        # 在 file_to_unit 的 keys 中搜索匹配
        for file_path, unit_name in file_to_unit.items():
            if file_path.endswith(target_file) or file_path.endswith('/' + target_file):
                return unit_name
        return None


# ============================================================
# 步骤 5: 拓扑排序
# ============================================================

def topological_sort(units: list[Unit], deps: dict[str, set[str]]) -> list[list[Unit]]:
    """将 unit 依赖图拓扑排序，返回分层列表（同层可并行翻译）"""
    name_to_unit = {u.name: u for u in units}

    # 构建邻接表：dep_A → 依赖它的那些 unit
    # 边方向：A 依赖 B → 翻译顺序 B 在 A 之前 → 边 B→A
    adj = defaultdict(set)
    in_degree = defaultdict(int)

    for u in units:
        in_degree[u.name] = in_degree.get(u.name, 0)
        for dep_name in deps.get(u.name, set()):
            if dep_name in name_to_unit:
                adj[dep_name].add(u.name)
                in_degree[u.name] += 1

    # BFS 分层
    queue = deque()
    for u in units:
        if in_degree[u.name] == 0:
            queue.append((u.name, 0))

    depth_map: dict[str, int] = {}
    while queue:
        name, depth = queue.popleft()
        if name in depth_map:
            continue
        depth_map[name] = depth
        for neighbor in adj[name]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append((neighbor, depth + 1))

    # 处理未到达的（循环依赖）
    for u in units:
        if u.name not in depth_map:
            depth_map[u.name] = max(depth_map.values()) + 1 if depth_map else 0

    # 按深度分组
    max_depth = max(depth_map.values()) if depth_map else 0
    layers: list[list[Unit]] = [[] for _ in range(max_depth + 1)]
    for u in units:
        layers[depth_map[u.name]].append(u)

    return layers


# ============================================================
# 主编排器
# ============================================================

class OrderDeterminer:
    """翻译顺序确定 — 编排整个流程"""

    def __init__(self, project_path: str, cache_dir: str = ".pipeline_cache"):
        self.project_path = Path(project_path)
        self.cache_dir = self.project_path / cache_dir
        self.llm = LLMClient()
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

        # 3. 划分 Unit
        print(f"\n[3/5] LLM 划分翻译单元...")
        tools = ToolRegistry(str(self.project_path), self.summaries)
        unit_builder = UnitBuilder(self.llm, tools)
        units = unit_builder.build_units(java_files, xml_files)

        print(f"  划分结果: {len(units)} 个单元")
        for u in units:
            names = [Path(s).name for s in u.sources]
            print(f"    [{u.name}] → {names}")

        # 4. 分析 Unit 依赖
        print(f"\n[4/5] 分析 Unit 依赖...")
        dep_analyzer = UnitDependencyAnalyzer(self.llm, tools, self.summaries)
        unit_deps = dep_analyzer.analyze(units)

        # 5. 拓扑排序
        print(f"\n[5/5] 拓扑排序...")
        layers = topological_sort(units, unit_deps)

        print(f"\n翻译顺序（分 {len(layers)} 层）:")
        for depth, layer in enumerate(layers):
            names = [u.name for u in layer]
            deps_info = ", ".join(
                f"→ {d}" for u in layer for d in sorted(unit_deps.get(u.name, set()))
            )[:100]
            print(f"  第{depth}层: {names}  {deps_info}")

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

"""LLM 可调用的工具 — 读文件、查摘要"""

from pathlib import Path
import json
from dataclasses import asdict
from .static import FileSummary, XmlSummary, analyze_file, JavaStaticAnalyzer, XmlStaticAnalyzer


def _summary_to_dict(s) -> dict:
    """将 FileSummary / XmlSummary 序列化为 JSON 友好的 dict"""
    if isinstance(s, FileSummary):
        return {
            "type": "java",
            "file": str(s.file_path),
            "class": s.class_name,
            "package": s.package,
            "class_type": s.class_type,
            "extends": s.extends,
            "implements": s.implements,
            "class_line": s.class_line,
            "project_imports": s.project_imports,
            "import_region": f"L{s.import_start}-L{s.import_end}",
            "android_imports_count": s.android_imports_count,
            "resource_refs": s.resource_refs,
            "lines": s.lines,
            "methods": [
                {
                    "name": m.name,
                    "sig": m.sig,
                    "region": f"L{m.start_line}-L{m.end_line} ({m.lines}行)",
                    "vis": m.vis,
                    "purpose": m.purpose,
                    "params": [{"name": p.name, "type": p.type, "purpose": p.purpose} for p in m.params],
                    "returns": {"type": m.returns.type, "meaning": m.returns.meaning} if m.returns else None,
                    "side_effects": m.side_effects,
                }
                for m in s.methods
            ],
            "fields": [
                {"name": f.name, "type": f.type, "vis": f.vis, "line": f.line, "purpose": f.purpose}
                for f in s.fields
            ],
            "features": {
                "has_lifecycle": s.has_lifecycle,
                "has_event_listeners": s.has_event_listeners,
                "has_findViewById": s.has_find_view_by_id,
            },
            "summary": {
                "class_purpose": s.class_purpose,
                "class_role": s.class_role,
                "design_pattern": s.design_pattern,
                "is_stateful": s.is_stateful,
                "lifecycle_dependent": s.lifecycle_dependent,
                "call_flow": s.call_flow,
            },
        }
    elif isinstance(s, XmlSummary):
        return {
            "type": "xml",
            "file": str(s.file_path),
            "xml_type": s.xml_type,
            "root": s.root_widget,
            "orientation": s.root_orientation,
            "widgets": s.widgets,
            "ids": s.ids,
            "resource_refs": s.resource_refs,
            "lines": s.lines,
            "summary": {
                "purpose": s.purpose,
                "pattern": s.layout_pattern,
                "hierarchy": s.hierarchy,
                "data_binding": s.data_binding,
                "event_handling": s.event_handling,
            },
        }
    return {"type": "unknown"}


class ToolRegistry:
    """工具注册表 — 管理 LLM 可调用的工具"""

    def __init__(self, project_root: str, summaries: dict[str, FileSummary | XmlSummary],
                 resource_mapping_path: str | None = None):
        self.project_root = Path(project_root)
        self.summaries = summaries  # file_path -> summary
        self.resource_mapping: dict | None = None
        if resource_mapping_path and Path(resource_mapping_path).exists():
            import json
            with open(resource_mapping_path, 'r') as f:
                self.resource_mapping = json.load(f)

    def get_tool_schemas(self) -> list[dict]:
        """返回 OpenAI function calling 格式的工具定义"""
        return [
            {
                "type": "function",
                "function": {
                    "name": "get_all_summaries",
                    "description": "获取项目中所有文件的摘要列表。用于了解项目全貌、找到需要分析的文件。",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_file_summary",
                    "description": "获取指定文件的详细摘要，包括类信息、方法签名、字段、资源引用等。优先使用此工具而非直接读文件。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {
                                "type": "string",
                                "description": "文件路径的结尾部分，如 'DbHelper.java' 或 'MainActivity.java'，不需要完整路径",
                            },
                        },
                        "required": ["file_path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_file_region",
                    "description": "读取文件的指定行范围。仅在摘要不足以判断时使用，不要为了解基本情况而调用。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {
                                "type": "string",
                                "description": "文件路径的结尾部分，如 'MainActivity.java'",
                            },
                            "start_line": {
                                "type": "integer",
                                "description": "起始行号（1-based）",
                            },
                            "end_line": {
                                "type": "integer",
                                "description": "结束行号（1-based）",
                            },
                        },
                        "required": ["file_path", "start_line", "end_line"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_full_file",
                    "description": "读取完整文件。仅在确实需要查看完整代码时调用（如翻译阶段），避免不必要的 token 消耗。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {
                                "type": "string",
                                "description": "文件路径的结尾部分，如 'MainActivity.java'",
                            },
                        },
                        "required": ["file_path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "lookup_resource",
                    "description": "查询 Android 资源在 HarmonyOS 工程中的位置和值。如 lookup_resource('R.string.app_name') 返回其 HarmonyOS 路径和值。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "ref": {
                                "type": "string",
                                "description": "Android 资源引用，如 'R.string.app_name' / '@string/app_name' / 'R.drawable.icon'",
                            },
                        },
                        "required": ["ref"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_resource_mapping",
                    "description": "列出所有已迁移的静态资源摘要，包括类型、原始引用和 HarmonyOS 位置。",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            },
        ]

    # ---- 工具实现 ----

    def get_all_summaries(self) -> str:
        """列出所有文件的摘要"""
        items = []
        for file_path, s in sorted(self.summaries.items()):
            if isinstance(s, FileSummary):
                methods_count = len(s.methods)
                items.append(
                    f"[java] {Path(file_path).name} | class={s.class_name}, type={s.class_type}, "
                    f"extends={s.extends}, methods={methods_count}, lines={s.lines}, "
                    f"imports={len(s.project_imports)}, layout_refs={s.resource_refs.get('layout', [])}"
                )
            elif isinstance(s, XmlSummary):
                items.append(
                    f"[xml]  {Path(file_path).name} | root={s.root_widget}, "
                    f"widgets={s.widgets}, ids={s.ids}, lines={s.lines}"
                )
        return "\n".join(items)

    def get_file_summary(self, file_path: str) -> str:
        """获取文件详细摘要"""
        # 模糊匹配文件名
        match = self._find_file(file_path)
        if not match:
            return f"错误：未找到匹配文件 '{file_path}'。可用的文件有：{self._list_files()}"

        s = self.summaries[match]
        d = _summary_to_dict(s)
        return json.dumps(d, ensure_ascii=False, indent=2)

    def read_file_region(self, file_path: str, start_line: int, end_line: int) -> str:
        """读文件指定区域"""
        match = self._find_file(file_path)
        if not match:
            return f"错误：未找到文件 '{file_path}'"

        full_path = self.project_root / match
        if not full_path.exists():
            # 尝试直接作为绝对路径
            full_path = Path(file_path)
            if not full_path.exists():
                return f"错误：文件不存在 '{file_path}'"

        try:
            with open(full_path, 'r', encoding='utf-8') as f:
                all_lines = f.readlines()

            start = max(0, start_line - 1)
            end = min(len(all_lines), end_line)

            result_lines = []
            for i in range(start, end):
                result_lines.append(f"L{i + 1:4d}: {all_lines[i].rstrip()}")

            return f"=== {match} L{start_line}-L{end_line} ===\n" + "\n".join(result_lines)
        except Exception as e:
            return f"错误：读取文件失败 - {e}"

    def read_full_file(self, file_path: str) -> str:
        """读完整文件"""
        match = self._find_file(file_path)
        if not match:
            return f"错误：未找到文件 '{file_path}'"

        full_path = self.project_root / match
        try:
            with open(full_path, 'r', encoding='utf-8') as f:
                content = f.read()
            return f"=== {match} (完整) ===\n{content}"
        except Exception as e:
            return f"错误：读取文件失败 - {e}"

    def _find_file(self, partial: str) -> str | None:
        """模糊匹配文件路径"""
        partial_lower = partial.lower()

        # 精确匹配
        for file_path in self.summaries:
            if file_path == partial:
                return file_path
            if file_path.lower() == partial_lower:
                return file_path

        # 文件名结尾匹配
        candidates = []
        for file_path in self.summaries:
            if file_path.lower().endswith(partial_lower):
                candidates.append(file_path)

        if len(candidates) == 1:
            return candidates[0]
        elif len(candidates) > 1:
            # 返回最短的（最精确的）
            return min(candidates, key=len)

        # 包含匹配
        for file_path in self.summaries:
            if partial_lower in file_path.lower():
                return file_path

        return None

    def _list_files(self) -> str:
        return ", ".join(Path(p).name for p in self.summaries)

    def lookup_resource(self, ref: str) -> str:
        """查询 Android 资源的 HarmonyOS 迁移位置"""
        if not self.resource_mapping:
            return "错误：资源映射未加载"

        # 标准化 ref 格式：@string/xxx → R.string.xxx
        ref = ref.strip()
        if ref.startswith("@"):
            parts = ref[1:].split("/", 1)
            if len(parts) == 2:
                ref = f"R.{parts[0]}.{parts[1]}"

        by_ref = self.resource_mapping.get("by_android_ref", {})
        entry = by_ref.get(ref)
        if entry:
            return json.dumps(entry, ensure_ascii=False, indent=2)

        # 模糊匹配
        ref_lower = ref.lower()
        for key, entry in by_ref.items():
            if ref_lower in key.lower():
                return json.dumps(entry, ensure_ascii=False, indent=2)

        return f"未找到资源 '{ref}'。可用的资源引用有：{', '.join(list(by_ref.keys())[:20])}"

    def list_resource_mapping(self) -> str:
        """列出所有资源映射"""
        if not self.resource_mapping:
            return "错误：资源映射未加载"

        entries = self.resource_mapping.get("entries", [])
        lines = [f"共 {len(entries)} 个资源已迁移：\n"]
        by_type = {}
        for e in entries:
            t = e.get("resource_type", "unknown")
            by_type.setdefault(t, []).append(e)

        for t, items in sorted(by_type.items()):
            lines.append(f"## {t} ({len(items)} 项)")
            for item in items:
                lines.append(f"  {item['android_ref']} → {item['harmony_path']} #{item['harmony_key']} = '{item.get('value', '')[:50]}'")

        return "\n".join(lines)

    def execute(self, tool_name: str, arguments: dict) -> str:
        """执行工具调用"""
        method_map = {
            "get_all_summaries": lambda: self.get_all_summaries(),
            "get_file_summary": lambda: self.get_file_summary(arguments.get("file_path", "")),
            "read_file_region": lambda: self.read_file_region(
                arguments.get("file_path", ""),
                arguments.get("start_line", 1),
                arguments.get("end_line", 1),
            ),
            "read_full_file": lambda: self.read_full_file(arguments.get("file_path", "")),
            "lookup_resource": lambda: self.lookup_resource(arguments.get("ref", "")),
            "list_resource_mapping": lambda: self.list_resource_mapping(),
        }

        fn = method_map.get(tool_name)
        if fn:
            return fn()
        return f"错误：未知工具 '{tool_name}'"


def create_default_tools(project_root: str, summaries: dict) -> ToolRegistry:
    """创建默认工具注册表"""
    return ToolRegistry(project_root, summaries)

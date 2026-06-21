"""静态分析器 — 基于 javalang AST + 正则，零 token 消耗"""

import re
from pathlib import Path
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

import javalang


# ============================================================
# 数据模型
# ============================================================

@dataclass
class ParamSummary:
    name: str
    type: str
    purpose: str = ""  # LLM 填充

@dataclass
class ReturnSummary:
    type: str
    meaning: str = ""  # LLM 填充

@dataclass
class MethodSummary:
    name: str
    sig: str
    start_line: int
    end_line: int
    lines: int
    vis: str
    purpose: str = ""  # LLM 填充
    params: list[ParamSummary] = field(default_factory=list)
    returns: ReturnSummary | None = None  # LLM 填充
    side_effects: list[str] = field(default_factory=list)  # LLM 填充

@dataclass
class FieldSummary:
    name: str
    type: str
    vis: str
    line: int
    purpose: str = ""  # LLM 填充

@dataclass
class FileSummary:
    """文件摘要 — 静态自动提取 + LLM 语义补充"""
    file_path: str

    # 身份
    class_name: str = ""
    package: str = ""
    class_type: str = ""  # activity | fragment | adapter | entity | util | service | interface | other
    extends: str = ""
    implements: list[str] = field(default_factory=list)
    class_line: int = 0

    # 引用
    project_imports: list[str] = field(default_factory=list)
    import_start: int = 0
    import_end: int = 0
    android_imports_count: int = 0

    # 资源引用
    resource_refs: dict[str, list[str]] = field(default_factory=dict)

    # 结构
    lines: int = 0
    methods: list[MethodSummary] = field(default_factory=list)
    fields: list[FieldSummary] = field(default_factory=list)

    # 特征标记
    has_lifecycle: bool = False
    has_event_listeners: bool = False
    has_find_view_by_id: bool = False

    # LLM 生成（后续填充）
    class_purpose: str = ""
    class_role: str = ""
    design_pattern: str = ""
    is_stateful: bool = False
    lifecycle_dependent: bool = False
    call_flow: str = ""


# ============================================================
# Java 静态分析
# ============================================================

class JavaStaticAnalyzer:
    """基于 javalang AST 的 Java 文件静态分析器"""

    # 生命周期方法名
    LIFECYCLE_METHODS = {
        "onCreate", "onStart", "onResume", "onPause", "onStop",
        "onDestroy", "onRestart", "onSaveInstanceState",
        "onRestoreInstanceState", "onCreateView", "onDestroyView",
        "onAttach", "onDetach", "onCreateOptionsMenu",
    }

    # 事件监听器特征
    EVENT_PATTERNS = [
        r"setOn\w+Listener", r"addOn\w+Listener",
        r"addTextChangedListener", r"OnClick",
    ]

    @classmethod
    def analyze(cls, file_path: str, code: str) -> FileSummary:
        """分析单个 Java 文件"""
        s = FileSummary(file_path=file_path)
        s.lines = code.count('\n') + 1

        lines_list = code.split('\n')

        try:
            tree = javalang.parse.parse(code)
        except javalang.parser.JavaSyntaxError:
            # 解析失败，能用正则提取的部分还是做
            tree = None

        if tree:
            cls._extract_package(tree, s)
            cls._extract_imports(tree, s)
            cls._extract_class_info(tree, lines_list, s)
            cls._extract_methods(tree, lines_list, s)
            cls._extract_fields(tree, lines_list, s)

        # 正则提取（不依赖 AST）
        cls._extract_resources(code, s)
        cls._detect_features(code, s)

        return s

    @classmethod
    def _extract_package(cls, tree, s: FileSummary):
        if tree.package:
            s.package = tree.package.name

    @classmethod
    def _extract_imports(cls, tree, s: FileSummary):
        project_imports = []
        import_lines = []

        for imp in tree.imports:
            path = imp.path
            if path.startswith(('java.', 'javax.', 'android.', 'androidx.', 'org.w3c.')):
                s.android_imports_count += 1
            else:
                project_imports.append(path)

            if imp.position:
                import_lines.append(imp.position.line)

        s.project_imports = project_imports
        if import_lines:
            s.import_start = min(import_lines)
            s.import_end = max(import_lines)

    @classmethod
    def _extract_class_info(cls, tree, lines_list: list[str], s: FileSummary):
        for t in tree.types:
            if isinstance(t, javalang.tree.ClassDeclaration):
                s.class_name = t.name
                s.class_line = t.position.line if t.position else 0

                if t.extends:
                    s.extends = t.extends.name

                if t.implements:
                    s.implements = [i.name for i in t.implements]

                # 推断 class_type
                s.class_type = cls._infer_class_type(t, s)

    @classmethod
    def _infer_class_type(cls, t: javalang.tree.ClassDeclaration, s: FileSummary) -> str:
        extends_lower = s.extends.lower()
        if 'activity' in extends_lower:
            return 'activity'
        if 'fragment' in extends_lower:
            return 'fragment'
        if 'adapter' in extends_lower or 'adapter' in t.name.lower():
            return 'adapter'
        if 'service' in extends_lower:
            return 'service'
        if 'broadcastreceiver' in extends_lower:
            return 'receiver'
        if 'application' in extends_lower:
            return 'application'
        if 'view' in extends_lower and 'viewgroup' not in extends_lower:
            return 'view'
        if s.extends == '' and not s.implements:
            return 'entity'
        if 'util' in s.package.lower() or 'utils' in s.package.lower() or t.name.lower().endswith('util'):
            return 'util'
        return 'other'

    @classmethod
    def _extract_methods(cls, tree, lines_list: list[str], s: FileSummary):
        for t in tree.types:
            if isinstance(t, javalang.tree.ClassDeclaration):
                for member in t.body:
                    if isinstance(member, javalang.tree.MethodDeclaration):
                        m = cls._parse_method(member, lines_list)
                        if m:
                            s.methods.append(m)

    @classmethod
    def _parse_method(cls, node: javalang.tree.MethodDeclaration, lines_list: list[str]) -> MethodSummary | None:
        name = node.name
        vis = cls._get_visibility(node.modifiers)
        params = [
            ParamSummary(name=p.name, type=p.type.name if p.type else "unknown")
            for p in node.parameters
        ]
        ret_type = node.return_type.name if node.return_type else "void"
        params_str = ", ".join(f"{p.type} {p.name}" for p in params)
        sig = f"{vis} {ret_type} {name}({params_str})"

        # 确定方法的起止行
        start = node.position.line if node.position else 0

        # 找方法体的结束行
        end = start
        if node.body and node.position:
            # javalang 不直接给结束行，用大括号匹配
            brace_depth = 0
            in_method = False
            for i in range(start - 1, len(lines_list)):
                line = lines_list[i]
                brace_depth += line.count('{') - line.count('}')
                if '{' in line:
                    in_method = True
                if in_method and brace_depth == 0:
                    end = i + 1
                    break
                if brace_depth < 0:
                    end = i + 1
                    break

        lines = end - start + 1 if end >= start else 1

        m = MethodSummary(
            name=name, sig=sig, start_line=start, end_line=end,
            lines=lines, vis=vis,
            params=params,
            returns=ReturnSummary(type=ret_type),
        )
        return m

    @classmethod
    def _extract_fields(cls, tree, lines_list: list[str], s: FileSummary):
        for t in tree.types:
            if isinstance(t, javalang.tree.ClassDeclaration):
                for member in t.body:
                    if isinstance(member, javalang.tree.FieldDeclaration):
                        vis = cls._get_visibility(member.modifiers)
                        ftype = member.type.name if member.type else "unknown"
                        line = member.position.line if member.position else 0
                        for decl in member.declarators:
                            s.fields.append(FieldSummary(
                                name=decl.name, type=ftype, vis=vis, line=line
                            ))

    @classmethod
    def _get_visibility(cls, modifiers: set[str]) -> str:
        if 'public' in modifiers:
            return 'public'
        if 'protected' in modifiers:
            return 'protected'
        if 'private' in modifiers:
            return 'private'
        return 'package'

    @classmethod
    def _extract_resources(cls, code: str, s: FileSummary):
        """正则提取 R.xxx.* 资源引用"""
        refs: dict[str, set[str]] = {}

        patterns = {
            'layout':   r'R\.layout\.(\w+)',
            'id':       r'R\.id\.(\w+)',
            'string':   r'R\.string\.(\w+)',
            'drawable': r'R\.drawable\.(\w+)',
            'color':    r'R\.color\.(\w+)',
            'dimen':    r'R\.dimen\.(\w+)',
            'menu':     r'R\.menu\.(\w+)',
            'anim':     r'R\.anim\.(\w+)',
        }

        for key, pattern in patterns.items():
            matches = set(re.findall(pattern, code))
            if matches:
                refs[key] = sorted(matches)

        s.resource_refs = refs

    @classmethod
    def _detect_features(cls, code: str, s: FileSummary):
        """检测特征标记"""
        # 生命周期方法
        for m in s.methods:
            if m.name in cls.LIFECYCLE_METHODS:
                s.has_lifecycle = True
                s.lifecycle_dependent = True
                break

        # 事件监听器
        for pattern in cls.EVENT_PATTERNS:
            if re.search(pattern, code):
                s.has_event_listeners = True
                break

        # findViewById
        if re.search(r'findViewById', code):
            s.has_find_view_by_id = True


# ============================================================
# XML 静态分析
# ============================================================

@dataclass
class XmlSummary:
    file_path: str
    xml_type: str = ""  # layout | drawable | values | menu
    root_widget: str = ""
    root_orientation: str = ""
    widgets: list[str] = field(default_factory=list)
    ids: list[str] = field(default_factory=list)
    resource_refs: dict[str, list[str]] = field(default_factory=dict)
    lines: int = 0
    purpose: str = ""  # LLM 填充
    layout_pattern: str = ""  # LLM 填充
    hierarchy: str = ""  # LLM 填充
    data_binding: list[str] = field(default_factory=list)  # LLM 填充
    event_handling: list[str] = field(default_factory=list)  # LLM 填充

    def is_empty(self) -> bool:
        return not self.root_widget and not self.widgets


class XmlStaticAnalyzer:
    """XML 布局文件静态分析器"""

    ANDROID_NS = "http://schemas.android.com/apk/res/android"

    @classmethod
    def analyze(cls, file_path: str, code: str) -> XmlSummary:
        s = XmlSummary(file_path=file_path)
        s.lines = code.count('\n') + 1

        # 推断类型
        path = file_path.replace('\\', '/').lower()
        if 'layout' in path:
            s.xml_type = 'layout'
        elif 'drawable' in path:
            s.xml_type = 'drawable'
        elif 'values' in path:
            s.xml_type = 'values'
        elif 'menu' in path:
            s.xml_type = 'menu'

        try:
            root = ET.fromstring(code)
        except ET.ParseError:
            return s

        # Root widget
        tag = root.tag.split('}')[-1] if '}' in root.tag else root.tag
        s.root_widget = tag

        # Orientation
        orient = root.attrib.get(f"{{{cls.ANDROID_NS}}}orientation", "")
        if orient:
            s.root_orientation = orient

        # 收集所有 widgets 和 ids
        all_widgets = set()
        all_ids: list[str] = []
        cls._walk_xml(root, all_widgets, all_ids)

        s.widgets = sorted(all_widgets)
        s.ids = all_ids

        # 资源引用
        s.resource_refs = cls._extract_xml_refs(code)

        return s

    @classmethod
    def _walk_xml(cls, element, widgets: set, ids: list):
        tag = element.tag.split('}')[-1] if '}' in element.tag else element.tag
        widgets.add(tag)

        wid = element.attrib.get(f"{{{cls.ANDROID_NS}}}id", "")
        if wid and wid.startswith("@+id/"):
            ids.append(wid[5:])  # 去掉 @+id/ 前缀

        for child in element:
            cls._walk_xml(child, widgets, ids)

    @classmethod
    def _extract_xml_refs(cls, code: str) -> dict[str, list[str]]:
        refs: dict[str, set[str]] = {}
        patterns = {
            'drawable': r'@drawable/(\w+)',
            'string':   r'@string/(\w+)',
            'color':    r'@color/(\w+)',
            'dimen':    r'@dimen/(\w+)',
            'layout':   r'@layout/(\w+)',
            'menu':     r'@menu/(\w+)',
            'anim':     r'@anim/(\w+)',
        }
        for key, pattern in patterns.items():
            matches = set(re.findall(pattern, code))
            if matches:
                refs[key] = sorted(matches)
        return refs


# ============================================================
# 便捷函数
# ============================================================

def analyze_file(file_path: str, code: str) -> FileSummary | XmlSummary | None:
    """自动判断文件类型并分析"""
    if file_path.endswith('.java'):
        return JavaStaticAnalyzer.analyze(file_path, code)
    elif file_path.endswith('.xml'):
        return XmlStaticAnalyzer.analyze(file_path, code)
    return None

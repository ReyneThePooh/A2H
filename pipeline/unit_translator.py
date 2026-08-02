"""Unit 翻译器 — Planner 决策 + 自动组装源码 → Executor 翻译"""

import json
import os
from pathlib import Path
from dataclasses import dataclass, field
from openai import OpenAI

from analyzers.tools import ToolRegistry, _summary_to_dict
from pipeline.order_determiner import Unit


# ============================================================
# LLM 客户端
# ============================================================

class LLMClient:
    def __init__(self):
        self.client = OpenAI(
            api_key=os.getenv("LLM_API_KEY"),
            base_url=os.getenv("LLM_BASE_URL"),
            timeout=int(os.getenv("LLM_TIMEOUT", "180")),
        )
        self.model = os.getenv("LLM_MODEL_ID", "deepseek-v4-flash")

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             temperature: float = 0.3, **kwargs) -> dict:
        kw = dict(model=self.model, messages=messages, temperature=temperature, **kwargs)
        if tools:
            kw["tools"] = tools
        response = self.client.chat.completions.create(**kw)
        msg = response.choices[0].message
        return {
            "content": msg.content or "",
            "tool_calls": [
                {"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments}
                for tc in (msg.tool_calls or [])
            ],
        }

    def invoke(self, messages: list[dict], temperature: float = 0.3, **kwargs) -> str:
        """无工具调用，直接返回文本"""
        kw = dict(model=self.model, messages=messages, temperature=temperature, **kwargs)
        response = self.client.chat.completions.create(**kw)
        return response.choices[0].message.content or ""

    def chat_with_tools(self, messages: list[dict], tools: list[dict],
                        tool_executor, max_rounds: int = 8) -> str:
        for _ in range(max_rounds):
            result = self.chat(messages, tools=tools)
            if result["tool_calls"]:
                assistant_msg: dict = {"role": "assistant", "content": result["content"]}
                tool_blocks = [
                    {"id": tc["id"], "type": "function",
                     "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                    for tc in result["tool_calls"]
                ]
                assistant_msg["tool_calls"] = tool_blocks
                messages.append(assistant_msg)
                for tc in result["tool_calls"]:
                    tool_result = tool_executor(tc["name"], json.loads(tc["arguments"]))
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": tool_result})
            else:
                return result["content"]
        return result["content"]


# ============================================================
# 数据模型
# ============================================================

@dataclass
class ReadRegion:
    """Planner 指定的源码读取区域"""
    file: str      # 文件名（如 "MainActivity.java"）
    lines: str     # "L1-L35" 或 "L47-L85"


@dataclass
class PlanStep:
    step: str
    read_regions: list[dict] = field(default_factory=list)


@dataclass
class OutputPlan:
    file: str
    order: int
    type: str           # "simple" | "planned"
    sources: list[str]
    plan: list[dict] = field(default_factory=list)  # [{"step":..., "read_regions":[...]}]
    depends_on: list[str] = field(default_factory=list)
    description: str = ""


@dataclass
class TranslationResult:
    unit_name: str = ""
    file_name: str = ""
    code: str = ""
    address: str = ""
    success: bool = False
    error: str = ""


# ============================================================
# Planner
# ============================================================

PLANNER_PROMPT = """你是一个 Android → HarmonyOS 代码翻译规划专家。

## 任务
分析翻译单元中的源文件，决定:
1. 输出几个 .ets 文件及其顺序
2. 每个文件分几步翻译
3. 每一步需要读取哪些源文件的哪些行

## 可用工具
- get_all_summaries(): 列出项目文件
- get_file_summary(文件名): 查看文件详细摘要（方法签名、行号、字段、资源引用等）

## 决策规则

### 简单文件 → type="simple", plan 为空
- 单文件 Java（工具类/数据模型），无关联 XML，行数 < 200
- 单个 XML 布局

### 复杂文件 → type="planned", plan 含 2-4 步
- Java Activity + XML 布局
- 多个关联文件需协同翻译

## read_regions 格式
摘要中每个方法、字段都有精确行号，请根据这些行号指定:
- "L1-L28"  (import 区域)
- "L30-L40" (类声明和字段)
- "L42-L87" (具体方法)

## 输出格式（严格 JSON）
{{
  "outputs": [
    {{
      "file": "DbHelper.ets",
      "order": 0,
      "type": "simple",
      "sources": ["DbHelper.java"],
      "depends_on": [],
      "description": "数据库工具类"
    }},
    {{
      "file": "MainPage.ets",
      "order": 0,
      "type": "planned",
      "sources": ["MainActivity.java", "activity_main.xml"],
      "plan": [
        {{
          "step": "翻译 import 和 @State 变量",
          "read_regions": [
            {{"file": "MainActivity.java", "lines": "L1-L35"}},
            {{"file": "activity_main.xml", "lines": "L1-L30"}}
          ]
        }},
        {{
          "step": "翻译 build() 方法",
          "read_regions": [
            {{"file": "activity_main.xml", "lines": "L1-L40"}}
          ]
        }}
      ],
      "depends_on": [],
      "description": "欢迎页面"
    }}
  ]
}}

只输出 JSON。"""


# ============================================================
# Executor
# ============================================================

SIMPLE_EXECUTOR_PROMPT = """你是一个 Java/XML → ArkTS 代码翻译专家。

## 翻译任务
{file_name}: {description}

## 源文件完整代码
{source_code}

## 依赖上下文（已翻译完成的单元接口）
{context}

## 资源映射（按需使用，lookup_resource 可查更多）
{resource_hints}

## 翻译规则
- 资源引用: R.string.xxx → $r('app.string.xxx'), R.color.xxx → $r('app.color.xxx')
- 组件: TextView→Text, EditText→TextInput, Button→Button, LinearLayout→Column/Row, ListView→List+ForEach
- 布局尺寸（必须遵守）:
  - 组件带 style="@style/xxx" 时，宽高/权重/字号/颜色以源码后附带的 style 定义为准，不得凭空猜测
  - android:layout_weight / layout_columnWeight → .layoutWeight(n)；layout_width="0dp" 配合 weight 表示按权重分配，不是固定宽度
  - Row 的直接子组件要均分宽度时用 .layoutWeight(1)，严禁设置 .width('100%')（会把整行挤爆）
  - GridLayout → 逐行 Row + 子组件 .layoutWeight(1)（或 Grid + columnsTemplate）；rowSpan/columnSpan 用嵌套 Row/Column + layoutWeight 还原
- 生命周期: onCreate→aboutToAppear, onDestroy→aboutToDisappear
- SQLite → relationalStore (@ohos.data.relationalStore)
- 若 Planner 给的行范围不足，可用 read_file_region 补充读取
- 可调用 lookup_resource(ref) 确认资源映射

## 输出格式（严格 JSON）
{{
  "code": "完整的 .ets 文件代码"
}}

只输出 JSON。"""


STEP_EXECUTOR_PROMPT = """你是一个 Java/XML → ArkTS 代码翻译专家。按计划逐步翻译。

## 文件: {file_name}
## 本步骤: {step_desc}
## 已完成步骤数: {step_index}/{total_steps}

## 本步骤相关的源码
{step_code}

## 已累积的 ArkTS 代码
{accumulated}

## 翻译规则
- 在当前累积代码基础上增量添加本步骤结果
- 第一步请从 import 开始完整输出结构声明
- 资源引用: R.string.xxx → $r('app.string.xxx')
- 布局尺寸（必须遵守）:
  - 组件带 style="@style/xxx" 时，宽高/权重/字号/颜色以源码后附带的 style 定义为准，不得凭空猜测
  - android:layout_weight / layout_columnWeight → .layoutWeight(n)；layout_width="0dp" 配合 weight 表示按权重分配，不是固定宽度
  - Row 的直接子组件要均分宽度时用 .layoutWeight(1)，严禁设置 .width('100%')（会把整行挤爆）
  - GridLayout → 逐行 Row + 子组件 .layoutWeight(1)（或 Grid + columnsTemplate）；rowSpan/columnSpan 用嵌套 Row/Column + layoutWeight 还原
- 若代码不够，可用 read_file_region 补充

## 输出格式（严格 JSON）
{{
  "code": "当前完整的 ArkTS 代码（增量后）",
  "done": false
}}

最后一步 done: true。
只输出 JSON。"""


# ============================================================
# 源码读取器（Python，非 LLM）
# ============================================================

class SourceReader:
    """根据 Planner 的 read_regions 组装源码"""

    def __init__(self, project_root: Path):
        self.root = project_root
        self._styles: dict[str, tuple[str, str]] | None = None  # name -> (parent, xml_text)

    def read_regions(self, regions: list[dict]) -> str:
        """读取指定区域，组装为标注文本。

        XML（布局/资源）文件一律全量注入：布局文件本身很短，
        只给片段会让 LLM 看不到完整结构，翻出破碎的 UI。
        """
        parts = []
        xml_seen: set[Path] = set()
        for r in regions:
            filename = r.get("file", "")
            lines_spec = r.get("lines", "")
            fpath = self._find(filename)
            if not fpath:
                parts.append(f"// [未找到文件: {filename}]")
                continue

            if fpath.suffix == ".xml":
                if fpath in xml_seen:
                    continue
                xml_seen.add(fpath)
                content = fpath.read_text(encoding='utf-8')
                parts.append(f"<!-- ===== {filename} (完整文件) ===== -->\n{content}\n")
                continue

            start, end = self._parse_lines(lines_spec, fpath)
            with open(fpath, 'r', encoding='utf-8') as f:
                all_lines = f.readlines()

            start = max(0, start - 1)
            end = min(len(all_lines), end)

            parts.append(f"// ===== {filename} L{start + 1}-L{end} =====\n")
            for i in range(start, end):
                parts.append(all_lines[i])

        text = "".join(parts)
        return text + self._style_context(text)

    def read_all(self, sources: list[str]) -> str:
        """读取所有源文件的完整内容"""
        parts = []
        for src in sources:
            fpath = self._find(Path(src).name) if "/" in src or "\\" in src else self._find(src)
            if not fpath:
                fpath = self._find(src)
            if not fpath:
                continue
            code = fpath.read_text(encoding='utf-8')
            ext = Path(src).suffix
            prefix = "//" if ext == ".java" else "<!--"
            suffix = "" if ext == ".java" else " -->"
            parts.append(f"{prefix} ===== {Path(src).name} ===== {suffix}\n{code}")
        text = "\n\n".join(parts)
        return text + self._style_context(text)

    # ---- style 上下文 ----

    def _style_context(self, content: str) -> str:
        """提取 content 中引用的 @style/Xxx，附上 res/values 里的 style 定义。

        Android 布局常把宽高/字号/颜色放在 style 里（如 layout_width=0dp +
        layout_columnWeight=1），不注入这些定义 LLM 只能瞎猜组件尺寸。
        """
        import re
        names = set(re.findall(r'@style/([\w.]+)', content))
        if not names:
            return ""
        styles = self._load_styles()
        blocks: list[str] = []
        emitted: set[str] = set()

        def emit(name: str):
            if name in emitted or name not in styles:
                return
            emitted.add(name)
            parent, xml_text = styles[name]
            blocks.append(xml_text)
            # 显式 parent 或者点号命名的隐式 parent（AppTheme.NoActionBar → AppTheme）
            if parent:
                emit(parent.split('/')[-1])
            elif '.' in name:
                emit(name.rsplit('.', 1)[0])

        for n in sorted(names):
            emit(n)
        if not blocks:
            return ""
        header = ("\n\n<!-- ===== 布局引用的 style 定义（来自 res/values，"
                  "组件的宽高/权重/字号/颜色以此为准）===== -->\n")
        return header + "\n".join(blocks)

    def _load_styles(self) -> dict[str, tuple[str, str]]:
        """扫描 res/values*/ 下所有 <style>，建立 name -> (parent, 原文) 索引"""
        if self._styles is not None:
            return self._styles
        from xml.etree import ElementTree as ET
        self._styles = {}
        for f in self.root.rglob("*.xml"):
            if not f.parent.name.startswith("values") or "build" in f.parts:
                continue
            try:
                root = ET.parse(f).getroot()
            except ET.ParseError:
                continue
            for style in root.findall("style"):
                name = style.attrib.get("name", "")
                if not name:
                    continue
                parent = style.attrib.get("parent", "")
                self._styles[name] = (parent, ET.tostring(style, encoding='unicode').strip())
        return self._styles

    def _find(self, filename: str) -> Path | None:
        name = Path(filename).name
        for d in [self.root, self.root / "app", self.root / "app/src/main"]:
            if not d.exists():
                continue
            for f in d.rglob(name):
                if f.is_file():
                    return f
        return None

    @staticmethod
    def _parse_lines(spec: str, fpath: Path) -> tuple[int, int]:
        """解析 'L1-L35' → (1, 35)，若无效则返回整个文件"""
        import re
        m = re.match(r'L?(\d+)-L?(\d+)', spec.replace(" ", ""))
        if m:
            return int(m.group(1)), int(m.group(2))
        total = sum(1 for _ in open(fpath, 'r', encoding='utf-8'))
        return 1, total


# ============================================================
# 翻译器
# ============================================================

class UnitTranslator:
    def __init__(self, llm: LLMClient, tools: ToolRegistry,
                 harmony_root: str, summaries: dict):
        self.llm = llm
        self.tools = tools
        self.harmony_root = Path(harmony_root)
        self.summaries = summaries
        self.reader = SourceReader(tools.project_root)

    def translate(self, unit: Unit, dep_summaries: str,
                  unit_output_cache: dict[str, str]) -> list[TranslationResult]:
        print(f"\n{'─' * 50}")
        print(f"翻译 Unit: {unit.name} ({len(unit.sources)} 文件)")
        print(f"{'─' * 50}")

        # 1. Planner: 只读摘要，输出方案（含 read_regions）
        outputs = self._plan(unit, dep_summaries)
        if not outputs:
            return [TranslationResult(unit_name=unit.name, error="Planner 失败")]

        total_llm_calls = sum(
            (len(o.plan) if o.type == "planned" else 1) for o in outputs
        )
        print(f"  → {len(outputs)} 个 .ets, 预计 {total_llm_calls} 次 LLM 调用")

        # 2. 按 order 翻译
        results = []
        context = dep_summaries or "无"

        for o in sorted(outputs, key=lambda x: x.order):
            # 打印 Planner 的方案
            if o.type == "planned":
                for i, p in enumerate(o.plan):
                    regions = p.get("read_regions", [])
                    rinfo = ", ".join(f"{r['file']}[{r['lines']}]" for r in regions)
                    print(f"    Plan[{i}]: {p['step'][:60]} ← {rinfo}")
            # 补充同 unit 内已翻译 output 的接口摘要
            for dep_file in o.depends_on:
                if dep_file in unit_output_cache:
                    context += f"\n\n## 同单元依赖: {dep_file}\n{unit_output_cache[dep_file]}"

            if o.type == "simple":
                result = self._translate_simple(o, context)
            else:
                result = self._translate_planned(o, context)

            results.append(result)
            if result.success:
                unit_output_cache[o.file] = self._api_summary(result.code)
                self._write(o.file, result.code)

        return results

    # ---- Planner ----

    def _plan(self, unit: Unit, dep_summaries: str) -> list[OutputPlan]:
        source_list = "\n".join(f"  - {s}" for s in unit.sources)
        prompt = f"""## 本 Unit 源文件
{source_list}

## 依赖单元摘要
{dep_summaries or '无'}

先用 get_all_summaries 和 get_file_summary 了解文件结构，然后输出翻译方案。"""

        try:
            result = self.llm.chat_with_tools(
                [{"role": "system", "content": PLANNER_PROMPT},
                 {"role": "user", "content": prompt}],
                tools=self.tools.get_tool_schemas(),
                tool_executor=lambda n, a: self.tools.execute(n, a),
                max_rounds=6,
            )
            data = _parse_json(result)
            if data:
                outputs = []
                for o in data.get("outputs", []):
                    plan_steps = []
                    for p in o.get("plan", []):
                        plan_steps.append({
                            "step": p.get("step", ""),
                            "read_regions": p.get("read_regions", []),
                        })
                    outputs.append(OutputPlan(
                        file=o.get("file", "Unknown.ets"),
                        order=o.get("order", 0),
                        type=o.get("type", "simple"),
                        sources=o.get("sources", []),
                        plan=plan_steps,
                        depends_on=o.get("depends_on", []),
                        description=o.get("description", ""),
                    ))
                if outputs:
                    return outputs
        except Exception as e:
            print(f"  ⚠️ Planner 失败: {e}")

        return [self._fallback_plan(unit)]

    def _fallback_plan(self, unit: Unit) -> OutputPlan:
        name = self._guess_filename(unit)
        return OutputPlan(file=name, order=0, type="simple",
                          sources=unit.sources, description="兜底方案")

    # ---- Simple 翻译 ----

    def _translate_simple(self, o: OutputPlan, context: str) -> TranslationResult:
        print(f"  {o.file} (simple)")
        source_code = self.reader.read_all(o.sources)
        resource_hints = self._resource_hints()

        prompt = _safe_format(SIMPLE_EXECUTOR_PROMPT,
                              file_name=o.file, description=o.description,
                              source_code=source_code, context=context,
                              resource_hints=resource_hints)

        try:
            # 先用 invoke（无工具），源码已在 prompt 里
            result = self.llm.invoke([{"role": "user", "content": prompt}])
            data = _parse_json(result)
            if not data:
                # 回退：带工具再试一次
                result = self.llm.chat_with_tools(
                    [{"role": "user", "content": prompt}],
                    tools=self.tools.get_tool_schemas(),
                    tool_executor=lambda n, a: self.tools.execute(n, a),
                    max_rounds=3,
                )
                data = _parse_json(result)
            if data and data.get("code"):
                print(f"    ✓ {len(data['code'])} 字符")
                return TranslationResult(unit_name="", file_name=o.file, code=data["code"], success=True)
            else:
                return TranslationResult(unit_name="", file_name=o.file,
                                         error=f"JSON解析失败: {(result or '')[:100]}")
        except Exception as e:
            return TranslationResult(unit_name="", file_name=o.file, error=str(e))

    # ---- Planned 翻译 ----

    def _translate_planned(self, o: OutputPlan, context: str) -> TranslationResult:
        if not o.plan:
            return self._translate_simple(o, context)

        print(f"  {o.file} (planned, {len(o.plan)} 步)")
        accumulated = ""
        total = len(o.plan)

        for i, p in enumerate(o.plan):
            step_desc = p.get("step", f"步骤{i + 1}")
            regions = p.get("read_regions", [])

            # Python 自动组装源码
            if regions:
                step_code = self.reader.read_regions(regions)
            else:
                # Planner 没给区域，读全部源码
                step_code = self.reader.read_all(o.sources)

            print(f"    [{i + 1}/{total}] {step_desc[:50]} ({len(step_code)} 字符源码)")

            prompt = _safe_format(STEP_EXECUTOR_PROMPT,
                file_name=o.file,
                step_desc=step_desc,
                step_index=i + 1,
                total_steps=total,
                step_code=step_code,
                accumulated=accumulated if accumulated else "无（第一步，请从 import 开始输出完整结构）",
            )

            try:
                # 先用 invoke（无工具），源码已组装好
                result = self.llm.invoke([{"role": "user", "content": prompt}])
                data = _parse_json(result)
                if not data:
                    # 回退：带工具
                    result = self.llm.chat_with_tools(
                        [{"role": "user", "content": prompt}],
                        tools=self.tools.get_tool_schemas(),
                        tool_executor=lambda n, a: self.tools.execute(n, a),
                        max_rounds=3,
                    )
                    data = _parse_json(result)
                if data:
                    accumulated = data.get("code", accumulated)
                    # done 只在最后一步生效：LLM 提前宣布完成会把剩余步骤
                    # （如事件处理/业务逻辑）整个跳过，产出只有 UI 没有功能的页面
                    if data.get("done") and i < total - 1:
                        print(f"      ⚠️ LLM 在第 {i + 1}/{total} 步提前返回 done，忽略并继续执行剩余步骤")
                else:
                    preview = (result or "空")[:200]
                    print(f"      ⚠️ 解析失败: [{preview}]")
            except Exception as e:
                print(f"      ⚠️ 异常: {e}")
                break

        if accumulated:
            print(f"    ✓ {len(accumulated)} 字符")
            return TranslationResult(unit_name="", file_name=o.file, code=accumulated, success=True)
        return TranslationResult(unit_name="", file_name=o.file, error="未产出代码")

    # ---- 辅助 ----

    def _resource_hints(self) -> str:
        if not self.tools.resource_mapping:
            return "无"
        entries = self.tools.resource_mapping.get("entries", [])
        hints = []
        for e in entries[:20]:
            hints.append(f"  {e['android_ref']} → {e['harmony_key']}")
        return "\n".join(hints)

    def _api_summary(self, code: str) -> str:
        lines = code.split('\n')
        return '\n'.join(line for line in lines[:60]
                         if line.strip().startswith(('import ', 'export ', '@Component',
                                                      '@Entry', 'struct ', '@Prop',
                                                      '@Link', '@State', '@Builder')))

    def _write(self, filename: str, code: str):
        ets_dir = self.harmony_root / "entry/src/main/ets/pages"
        ets_dir.mkdir(parents=True, exist_ok=True)
        (ets_dir / filename).write_text(code, encoding='utf-8')
        print(f"      → {ets_dir / filename}")

    def _guess_filename(self, unit: Unit) -> str:
        for s in unit.sources:
            if s.endswith('.java'):
                name = Path(s).stem
                return name[:-8] + 'Page.ets' if name.endswith('Activity') else name + '.ets'
        return f"{unit.name}.ets"


# ============================================================
# 翻译编排
# ============================================================

class TranslationPipeline:
    def __init__(self, project_root: str, harmony_root: str,
                 summaries: dict, resource_mapping_path: str):
        self.project_root = project_root
        self.llm = LLMClient()
        self.tools = ToolRegistry(project_root, summaries, resource_mapping_path)
        self.translator = UnitTranslator(self.llm, self.tools, harmony_root, summaries)
        self.summaries = summaries

    def run(self, layers: list[list[Unit]], deps: dict[str, set[str]]) -> list[TranslationResult]:
        all_results = []
        cache: dict[str, str] = {}

        for depth, layer in enumerate(layers):
            print(f"\n{'=' * 60}")
            print(f"第 {depth} 层 ({len(layer)} 个 unit)")
            print(f"{'=' * 60}")

            for unit in layer:
                dep_ctx = self._dep_context(unit, deps, cache)
                unit_results = self.translator.translate(unit, dep_ctx, {})
                all_results.extend(unit_results)

                codes = [r.code[:300] for r in unit_results if r.success]
                if codes:
                    cache[unit.name] = "\n".join(codes)

        return all_results

    def _dep_context(self, unit: Unit, deps: dict, cache: dict) -> str:
        dep_names = deps.get(unit.name, set())
        if not dep_names:
            return "无"
        parts = []
        for name in sorted(dep_names):
            parts.append(f"\n### {name}")
            if name in cache:
                parts.append(cache[name])
            else:
                for fp, s in self.summaries.items():
                    ds = _summary_to_dict(s)
                    if ds.get("type") == "java":
                        parts.append(f"  [{Path(fp).name}] {ds['class']}: {ds['summary'].get('class_purpose', '')}")
        return "\n".join(parts)


# ============================================================
# 工具函数
# ============================================================

def _parse_json(text: str) -> dict | None:
    if not text:
        return None
    text = text.strip()
    if "```" in text:
        parts = text.split("```")
        for i, part in enumerate(parts):
            if i % 2 == 1 and part.strip():
                inner = part.strip()
                if inner.startswith(("json", "JSON")):
                    inner = inner[4:].strip()
                text = inner
                break
    start = text.find('{')
    if start < 0:
        return None
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


def _safe_format(template: str, **kwargs) -> str:
    """安全的 format — 自动转义 kwargs 值中的 { 和 }"""
    escaped = {}
    for k, v in kwargs.items():
        if isinstance(v, str):
            escaped[k] = v.replace('{', '{{').replace('}', '}}')
        else:
            escaped[k] = v
    return template.format(**escaped)


# ============================================================
# 测试
# ============================================================

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    from pipeline.order_determiner import OrderDeterminer

    project = "/Users/kxh/Desktop/横向/sample-android-project"
    harmony = f"{project}/HarmonyProject"

    od = OrderDeterminer(project)
    layers, deps = od.run()

    pipeline = TranslationPipeline(project, harmony, od.summaries,
                                   f"{harmony}/.resource_mapping.json")

    # 翻译所有层
    for depth, layer in enumerate(layers):
        print(f"\n{'='*40} 第{depth}层 ({len(layer)} units) {'='*40}")
        for u in layer:
            dep_ctx = pipeline._dep_context(u, deps, {})
            results = pipeline.translator.translate(u, dep_ctx, {})
            for r in results:
                status = "✓" if r.success else f"✗ ({r.error[:60]})"
                print(f"  {r.file_name}: {status}")

"""Unit 翻译器 — Planner 决策 + 自动组装源码 → Executor 翻译"""

import hashlib
import json
import os
import re
from pathlib import Path
from dataclasses import dataclass, field, asdict
from run_control import BudgetExceeded, check_budget

from hello_agents.core.llm import HelloAgentsLLM

from analyzers.tools import ToolRegistry, _summary_to_dict
from pipeline.order_determiner import Unit, merge_cyclic_units, topological_sort
from pipeline.static_graph import layered_topological_sort
from pipeline.agents import (
    create_pipeline_llm, TranslationPlanAgent, UnitTranslateAgent,
)
from pipeline.artifacts import (
    ArtifactContractError, atomic_write_json, build_translation_manifest,
    content_hash, project_path, stable_unit_id, write_artifact_manifest,
)

TRANSLATION_CACHE_VERSION = 2
PLANNER_REPAIR_ATTEMPTS = 2  # 首次规划 + 1 次结构化修复
FINAL_COMPLETION_REPAIR_ATTEMPTS = 2


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
    status: str = "failed"
    sources: list[str] = field(default_factory=list)
    completed_steps: int = 0
    total_steps: int = 0


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
- 行为控件标识必须保留：Java 中出现的 R.id.xxx，目标端对应的实际交互控件、
  列表或状态控件必须设置 .id('xxx')。不得只把标识写在注释、变量名或外层无关容器上
- PopupWindow、Dialog、PopupMenu 等独立交互层的根容器必须设置
  .id('a2h_active_scope')，用于跨端识别当前交互作用域
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

## 依赖上下文
{context}

## 资源映射
{resource_hints}

## 已累积的 ArkTS 代码
{accumulated}

## 翻译规则
- 在当前累积代码基础上增量添加本步骤结果
- 第一步请从 import 开始完整输出结构声明
- 资源引用: R.string.xxx → $r('app.string.xxx')
- 行为控件标识必须保留：Java 中出现的 R.id.xxx，目标端对应的实际交互控件、
  列表或状态控件必须设置 .id('xxx')。不得只把标识写在注释、变量名或外层无关容器上
- PopupWindow、Dialog、PopupMenu 等独立交互层的根容器必须设置
  .id('a2h_active_scope')，用于跨端识别当前交互作用域
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
                raise FileNotFoundError(f"Required source not found: {filename}")

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
            fpath = self._find(src)
            if not fpath:
                raise FileNotFoundError(f"Required source not found: {src}")
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
        relative = filename.replace("\\", "/")
        # The scanner's paths are relative to app/src/main; prefer exact paths.
        for base in (self.root / "app/src/main", self.root / "src/main", self.root):
            candidate = project_path(base, relative)
            if candidate.is_file():
                return candidate
        matches = [path for path in self.root.rglob(Path(relative).name)
                   if path.is_file() and "build" not in path.parts
                   and ("/" not in relative or path.as_posix().endswith("/" + relative))]
        if len(matches) > 1:
            raise ArtifactContractError(f"Ambiguous source path: {filename}")
        return matches[0] if matches else None

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
    def __init__(self, llm: HelloAgentsLLM, tools: ToolRegistry,
                 harmony_root: str, summaries: dict):
        self.llm = llm
        self.tools = tools
        self.harmony_root = Path(harmony_root)
        self.summaries = summaries
        self.reader = SourceReader(tools.project_root)
        self.planner_agent = TranslationPlanAgent(llm, tools)
        self.translate_agent = UnitTranslateAgent(llm, tools)
        # 跨进程翻译缓存：同模型、同源码的单元重跑时直接复用译文
        self._trans_cache_path = (Path(tools.project_root)
                                  / ".pipeline_cache" / "unit_translations.json")
        self._trans_cache = self._load_trans_cache()

    # ---- 翻译缓存 ----

    def _load_trans_cache(self) -> dict:
        try:
            with open(self._trans_cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("entries", {}) if isinstance(data, dict) and data.get("schema_version") == TRANSLATION_CACHE_VERSION else {}
        except (OSError, json.JSONDecodeError, TypeError):
            return {}

    def _save_trans_cache(self):
        try:
            self._trans_cache_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self._trans_cache_path, {"schema_version": TRANSLATION_CACHE_VERSION,
                                                       "entries": self._trans_cache})
        except OSError as e:
            print(f"  ⚠️ 翻译缓存写入失败: {e}")

    def _trans_cache_key(self, unit: Unit, dep_summaries: str = "") -> str:
        """模型 ID + 源文件路径 + 源码内容 的哈希。

        不含 unit 名（LLM 划分可能改名）；源码或模型一变即失效。
        删除 .pipeline_cache/unit_translations.json 可强制全部重译。
        """
        payload = {"schema": TRANSLATION_CACHE_VERSION, "model": os.getenv("LLM_MODEL_ID", ""),
                   "model_temperature": getattr(self.llm, "temperature", None),
                   "engine": {name: content_hash((Path(__file__).parent / name).read_bytes())
                              for name in ("unit_translator.py", "agents.py", "artifacts.py")},
                   "sources": sorted(unit.sources), "source_code": self.reader.read_all(sorted(unit.sources)),
                   "description": unit.description, "dependencies": dep_summaries,
                   "resource_mapping": self.tools.resource_mapping,
                   "prompts": [SIMPLE_EXECUTOR_PROMPT, STEP_EXECUTOR_PROMPT, TranslationPlanAgent.SYSTEM_PROMPT],
                   "sdk": self._sdk_fingerprint_inputs()}
        return content_hash(json.dumps(payload, sort_keys=True, ensure_ascii=False))

    @staticmethod
    def _sdk_fingerprint_inputs() -> dict:
        configuration = {key: os.getenv(key, "") for key in
                         ("DEVECO_SDK_HOME", "HARMONY_SDK_HOME", "COMPILE_SDK_VERSION", "TARGET_SDK_VERSION")}
        metadata = {}
        # SDK package descriptors carry the installed component versions. Hash
        # their contents, not only the configured directory names.
        for key in ("DEVECO_SDK_HOME", "HARMONY_SDK_HOME"):
            configured = configuration[key]
            if not configured or not Path(configured).is_dir():
                continue
            root = Path(configured)
            for directory, dirs, files in os.walk(root):
                depth = len(Path(directory).relative_to(root).parts)
                if depth >= 3:
                    dirs[:] = []
                for name in files:
                    if name.lower() in {"oh-uni-package.json", "sdk-pkg.json", "source.properties", "version.txt", "version.json"}:
                        path = Path(directory) / name
                        metadata[key + ":" + path.relative_to(root).as_posix()] = content_hash(path.read_bytes())
        return {"configuration": configuration, "component_metadata": metadata}

    def translate(self, unit: Unit, dep_summaries: str,
                  unit_output_cache: dict[str, str]) -> list[TranslationResult]:
        print(f"\n{'─' * 50}")
        print(f"翻译 Unit: {unit.name} ({len(unit.sources)} 文件)")
        print(f"{'─' * 50}")

        # 0. 命中跨进程缓存 → 直接写盘返回，不花 LLM 调用
        try:
            cache_key = self._trans_cache_key(unit, dep_summaries)
        except (OSError, ArtifactContractError) as exc:
            return [TranslationResult(unit_name=unit.name, error=str(exc))]
        cached = self._trans_cache.get(cache_key) if cache_key else None
        if self._valid_cache_entry(cached):
            print(f"  ✓ 命中翻译缓存（{len(cached['outputs'])} 个文件），跳过 LLM 调用")
            results = []
            for out in cached["outputs"]:
                self._write(out["file"], out["code"])
                unit_output_cache[out["file"]] = self._api_summary(out["code"])
                results.append(TranslationResult(
                    unit_name=unit.name, file_name=out["file"],
                    code=out["code"], success=True, status="generated",
                    sources=out["sources"], completed_steps=out["completed_steps"], total_steps=out["total_steps"]))
            return results

        self._checkpoint_path = self._trans_cache_path.parent / "checkpoints" / (stable_unit_id(unit.sources) + ".json")
        self._checkpoint = {"schema_version": TRANSLATION_CACHE_VERSION, "fingerprint": cache_key,
                            "status": "candidate", "steps": {}, "completed_outputs": {}, "completed_hashes": {},
                            "planner_attempts": [], "executor_repairs": []}
        try:
            checkpoint = json.loads(self._checkpoint_path.read_text(encoding="utf-8"))
            if (isinstance(checkpoint, dict) and checkpoint.get("fingerprint") == cache_key
                    and checkpoint.get("schema_version") == TRANSLATION_CACHE_VERSION
                    and isinstance(checkpoint.get("steps"), dict)
                    and isinstance(checkpoint.get("completed_outputs"), dict)
                    and isinstance(checkpoint.get("completed_hashes"), dict)):
                self._checkpoint = checkpoint
        except (OSError, ValueError):
            pass
        # 1. Planner: 只读摘要，输出方案（含 read_regions）
        self._last_plan_error = ""
        try:
            from_checkpoint = "plan" in self._checkpoint
            outputs = ([OutputPlan(**value) for value in self._checkpoint["plan"]]
                       if from_checkpoint else self._plan(unit, dep_summaries))
            try:
                self._validate_plan(outputs, unit, unit_output_cache)
            except (ValueError, TypeError, KeyError, ArtifactContractError) as validation_exc:
                if not outputs:
                    raise
                plan_error = str(validation_exc)
                if from_checkpoint:
                    self._checkpoint.pop("plan", None)
                    self._checkpoint["steps"] = {}
                    self._checkpoint["completed_outputs"] = {}
                    self._checkpoint["completed_hashes"] = {}
                    self._save_checkpoint()
                    outputs = []
                repair_limit = 2 if "Output overwrites completed dependency" in plan_error else 1
                for repair_index in range(repair_limit):
                    first_error = f"PLANNER_PLAN_INVALID: {plan_error}"
                    print(f"  ↻ Planner 计划修复重试 ({repair_index + 1}/{repair_limit}): {first_error}")
                    dependency_hint = ""
                    if "Output overwrites completed dependency" in plan_error:
                        completed = ", ".join(sorted(unit_output_cache)) or "（无）"
                        dependency_hint = (
                            "\n以下文件已经由依赖单元完成，禁止再次放入 outputs；只能放入 depends_on："
                            f" {completed}"
                        )
                    feedback = (
                        "上一次方案通过了 JSON 解析，但没有通过本地计划校验。"
                        f"\n诊断：{first_error}{dependency_hint}"
                        "\n请重新规划并覆盖所有当前 Unit 源文件，修正文件名、依赖、顺序和步骤结构。"
                        "\n只输出符合要求的 JSON。"
                    )
                    outputs = self._plan_once(unit, dep_summaries, feedback)
                    self._record_planner_attempt(
                        len(self._checkpoint.get("planner_attempts", [])) + 1,
                        bool(outputs), self._last_plan_error or first_error)
                    try:
                        self._validate_plan(outputs, unit, unit_output_cache)
                        break
                    except (ValueError, TypeError, KeyError, ArtifactContractError) as repaired_exc:
                        plan_error = str(repaired_exc)
                        if outputs:
                            self._last_plan_error = f"PLANNER_PLAN_INVALID: {plan_error}"
                        if repair_index + 1 >= repair_limit:
                            raise
        except (ValueError, TypeError, KeyError, ArtifactContractError) as exc:
            detail = getattr(self, "_last_plan_error", "")
            reason = detail or str(exc)
            print(f"  ✗ 翻译规划失败: {reason}")
            status = ("plan_validation_failed"
                      if not detail or detail.startswith("PLANNER_PLAN_INVALID:")
                      else "planner_failed")
            return [TranslationResult(unit_name=unit.name, status=status,
                                      error=f"Invalid translation plan: {reason}")]
        if not outputs:
            return [TranslationResult(unit_name=unit.name, status="planner_failed",
                                      error="PLANNER_EMPTY_OUTPUTS: no output plan")]
        self._checkpoint["plan"] = [asdict(output) for output in outputs]
        self._save_checkpoint()

        total_llm_calls = sum(
            (len(o.plan) if o.type == "planned" else 1) for o in outputs
        )
        print(f"  → {len(outputs)} 个 .ets, 预计 {total_llm_calls} 次 LLM 调用")

        # 2. 按 order 翻译
        results = []
        for o in sorted(outputs, key=lambda x: x.order):
            check_budget()
            context = dep_summaries or "无"
            missing = [name for name in o.depends_on if name not in unit_output_cache]
            if missing:
                results.append(TranslationResult(unit_name=unit.name, file_name=o.file,
                    sources=o.sources, status="blocked_dependency", error=f"Incomplete output dependencies: {missing}"))
                continue
            # 打印 Planner 的方案
            if o.type == "planned":
                for i, p in enumerate(o.plan):
                    regions = p.get("read_regions", [])
                    rinfo = ", ".join(f"{r['file']}[{r['lines']}]" for r in regions)
                    print(f"    Plan[{i}]: {p['step'][:60]} ← {rinfo}")
            # 补充同 unit 内已翻译 output 的接口摘要
            for dep_file in o.depends_on:
                if dep_file in unit_output_cache:
                    context += f"\n\n## 已生成依赖: {dep_file}\n{unit_output_cache[dep_file]}"

            saved = self._checkpoint["completed_outputs"].get(o.file)
            try:
                if (self._valid_saved_result(saved)
                        and self._checkpoint["completed_hashes"].get(o.file) == content_hash(saved["code"])):
                    result = TranslationResult(**saved)
                elif o.type == "simple":
                    result = self._translate_simple(o, context)
                else:
                    result = self._translate_planned(o, context)
            except (OSError, ArtifactContractError, ValueError) as exc:
                result = TranslationResult(file_name=o.file, error=str(exc))
            result.unit_name = unit.name
            result.sources = o.sources

            results.append(result)
            if result.success:
                result.status = "generated"
                unit_output_cache[o.file] = self._api_summary(result.code)
                self._checkpoint["completed_outputs"][o.file] = asdict(result)
                self._checkpoint["completed_hashes"][o.file] = content_hash(result.code)
                self._save_checkpoint()

        # 整单元全部成功才入缓存；部分结果保留在步骤/文件检查点中。
        if cache_key and results and all(r.success for r in results):
            for result in results:
                self._write(result.file_name, result.code)
            self._trans_cache[cache_key] = {
                "status": "generated_candidate",
                "unit": unit.name,
                "plan": [asdict(output) for output in outputs],
                "plan_sha256": content_hash(json.dumps([asdict(output) for output in outputs], sort_keys=True)),
                "dependencies": dep_summaries,
                "outputs": [{"file": r.file_name, "code": r.code, "sha256": content_hash(r.code),
                             "sources": r.sources, "completed_steps": r.completed_steps, "total_steps": r.total_steps}
                            for r in results],
            }
            self._save_trans_cache()

        return results

    @staticmethod
    def _valid_saved_result(value) -> bool:
        return (isinstance(value, dict) and value.get("success") is True
                and isinstance(value.get("code"), str) and bool(value["code"].strip())
                and isinstance(value.get("total_steps"), int) and value["total_steps"] > 0
                and value.get("completed_steps") == value["total_steps"])

    @staticmethod
    def _valid_cache_entry(value) -> bool:
        if (not isinstance(value, dict) or value.get("status") != "generated_candidate"
                or not isinstance(value.get("outputs"), list) or not value["outputs"]
                or not isinstance(value.get("plan"), list)
                or value.get("plan_sha256") != content_hash(json.dumps(value["plan"], sort_keys=True))):
            return False
        for output in value["outputs"]:
            if (not isinstance(output, dict) or not isinstance(output.get("code"), str)
                    or not output["code"].strip() or output.get("sha256") != content_hash(output["code"])
                    or not isinstance(output.get("sources"), list)
                    or not isinstance(output.get("total_steps"), int) or output["total_steps"] < 1
                    or output.get("completed_steps") != output["total_steps"]):
                return False
            try:
                project_path(Path.cwd(), output["file"])
            except (ArtifactContractError, KeyError, TypeError, AttributeError):
                return False
        return True

    def _save_checkpoint(self) -> None:
        if getattr(self, "_checkpoint_path", None):
            atomic_write_json(self._checkpoint_path, self._checkpoint)

    def _validate_plan(self, outputs: list[OutputPlan], unit: Unit,
                       available_outputs: dict[str, str] | None = None) -> None:
        """Validate source coverage and resolve output order against verified dependencies."""
        if not outputs:
            raise ValueError("Planner produced no outputs")
        available = {name.casefold(): name for name in (available_outputs or {})}
        seen = set()
        covered = set()
        for output in outputs:
            if not isinstance(output.file, str) or not output.file.endswith(".ets") or output.file.casefold() in seen:
                raise ValueError(f"Invalid or duplicate output: {output.file}")
            if output.file.casefold() in available:
                raise ValueError(f"Output overwrites completed dependency: {output.file}")
            project_path(self.harmony_root, output.file)
            if (type(output.order) is not int or not isinstance(output.plan, list)
                    or not isinstance(output.type, str) or output.type not in {"simple", "planned"} or not isinstance(output.sources, list)
                    or not output.sources or any(not isinstance(source, str) for source in output.sources)
                    or not isinstance(output.depends_on, list)
                    or any(not isinstance(dep, str) for dep in output.depends_on)):
                raise ValueError(f"Invalid output schema: {output.file}")
            normalized = []
            for source in output.sources:
                candidates = [name for name in unit.sources if name == source or name.replace("\\", "/").endswith("/" + source.replace("\\", "/"))]
                if len(candidates) != 1:
                    raise ValueError(f"Source outside unit or ambiguous: {source}")
                normalized.append(candidates[0])
            output.sources = normalized
            covered.update(normalized)
            if output.type == "planned" and (not isinstance(output.plan, list) or not output.plan
                or any(not isinstance(step, dict) or not isinstance(step.get("step"), str) or not step["step"]
                       or not isinstance(step.get("read_regions", []), list) for step in output.plan)):
                raise ValueError(f"Planned output has missing steps: {output.file}")
            for step in output.plan:
                for region in step.get("read_regions", []):
                    if (not isinstance(region, dict) or not isinstance(region.get("file"), str)
                            or not isinstance(region.get("lines"), str)):
                        raise ValueError(f"Invalid source region: {output.file}")
                    filename = region["file"].replace("\\", "/")
                    if Path(filename).suffix.lower() not in {".java", ".xml"}:
                        raise ValueError(
                            f"Read region must reference Android source, got {region['file']}")
                    candidates = [source for source in output.sources
                                  if source == filename or source.replace("\\", "/").endswith("/" + filename)]
                    if len(candidates) != 1:
                        raise ValueError(
                            f"Read region source outside output sources or ambiguous: {region['file']}")
                    if self.reader._find(candidates[0]) is None:
                        raise ValueError(f"Read region source not found: {candidates[0]}")
                    region["file"] = candidates[0]
            seen.add(output.file.casefold())
        if covered != set(unit.sources):
            raise ValueError(f"Plan omitted sources: {sorted(set(unit.sources) - covered)}")

        local = {output.file.casefold(): output.file for output in outputs}
        known = {**available, **local}
        dependencies = {}
        for output in outputs:
            missing = [dep for dep in output.depends_on if dep.casefold() not in known]
            if missing:
                raise ValueError(f"Unknown output dependencies for {output.file}: {missing}")
            output.depends_on = list(dict.fromkeys(known[dep.casefold()] for dep in output.depends_on))
            if output.file in output.depends_on:
                raise ValueError(f"Output depends on itself: {output.file}")
            dependencies[output.file] = {dep for dep in output.depends_on if dep.casefold() in local}
        names = [output.file for output in sorted(outputs, key=lambda item: item.order)]
        layers, cycles = layered_topological_sort(names, dependencies)
        if cycles:
            raise ValueError(f"Cyclic output dependencies: {cycles}; extract a shared module")
        order = {name: index for index, name in enumerate(name for layer in layers for name in layer)}
        for output in outputs:
            output.order = order[output.file]

    # ---- Planner ----

    def _plan(self, unit: Unit, dep_summaries: str) -> list[OutputPlan]:
        feedback = ""
        for attempt in range(PLANNER_REPAIR_ATTEMPTS):
            outputs = self._plan_once(unit, dep_summaries, feedback)
            self._record_planner_attempt(attempt + 1, bool(outputs), self._last_plan_error)
            if outputs:
                return outputs
            if attempt + 1 >= PLANNER_REPAIR_ATTEMPTS or not self._planner_error_repairable(self._last_plan_error):
                break
            feedback = (
                "上一次规划未通过本地校验，请修正后重新输出。"
                f"\n诊断：{self._last_plan_error}"
                "\n只输出符合要求的 JSON，不要输出解释、Markdown 或源码。"
            )
            print(f"  ↻ Planner 结构化修复重试 ({attempt + 1}/{PLANNER_REPAIR_ATTEMPTS - 1})")
        return []

    @staticmethod
    def _planner_error_repairable(error: str) -> bool:
        return bool(error) and (
            error.startswith((
                "PLANNER_EMPTY_RESPONSE:", "PLANNER_INVALID_RESPONSE:",
                "PLANNER_INVALID_JSON:", "PLANNER_INVALID_SCHEMA:",
                "PLANNER_MISSING_OUTPUTS:", "PLANNER_EMPTY_OUTPUTS:",
            ))
        )

    def _record_planner_attempt(self, attempt: int, success: bool, error: str) -> None:
        checkpoint = getattr(self, "_checkpoint", None)
        if not isinstance(checkpoint, dict):
            return
        checkpoint.setdefault("planner_attempts", []).append({
            "attempt": attempt,
            "success": success,
            "error": error[:320] if isinstance(error, str) else "",
        })
        self._save_checkpoint()

    def _plan_once(self, unit: Unit, dep_summaries: str, feedback: str = "") -> list[OutputPlan]:
        self._last_plan_error = ""
        source_list = "\n".join(f"  - {s}" for s in unit.sources)
        prompt = f"""## 本 Unit 源文件
{source_list}

## 依赖单元摘要
{dep_summaries or '无'}

先用 get_all_summaries 和 get_file_summary 了解文件结构，然后输出翻译方案。
{feedback}"""

        try:
            result = self.planner_agent.run(prompt)
            if not isinstance(result, str):
                self._last_plan_error = (
                    f"PLANNER_INVALID_RESPONSE: expected text, got {type(result).__name__}"
                )
                print(f"  ⚠️ Planner 失败: {self._last_plan_error}")
                return []
            if not result.strip():
                self._last_plan_error = "PLANNER_EMPTY_RESPONSE: response length=0"
                print(f"  ⚠️ Planner 失败: {self._last_plan_error}")
                return []
            data, json_error = _parse_json_payload(result)
            if json_error:
                self._last_plan_error = (
                    f"PLANNER_INVALID_JSON: response length={len(result)} "
                    f"preview={_response_preview(result)}"
                )
                print(f"  ⚠️ Planner 失败: {self._last_plan_error}")
                return []
            if not isinstance(data, dict):
                self._last_plan_error = (
                    f"PLANNER_INVALID_SCHEMA: top-level JSON must be an object, "
                    f"got {type(data).__name__}"
                )
                print(f"  ⚠️ Planner 失败: {self._last_plan_error}")
                return []
            raw_outputs = data.get("outputs")
            if not isinstance(raw_outputs, list):
                keys = ",".join(sorted(str(key) for key in data.keys())) or "<none>"
                self._last_plan_error = f"PLANNER_MISSING_OUTPUTS: top-level keys=[{keys}]"
                print(f"  ⚠️ Planner 失败: {self._last_plan_error}")
                return []
            if not raw_outputs:
                self._last_plan_error = "PLANNER_EMPTY_OUTPUTS: outputs=[]"
                print(f"  ⚠️ Planner 失败: {self._last_plan_error}")
                return []

            outputs = []
            for index, o in enumerate(raw_outputs):
                if not isinstance(o, dict):
                    self._last_plan_error = (
                        f"PLANNER_INVALID_SCHEMA: outputs[{index}] must be an object, "
                        f"got {type(o).__name__}"
                    )
                    print(f"  ⚠️ Planner 失败: {self._last_plan_error}")
                    return []
                plan_steps = []
                raw_plan = o.get("plan", [])
                if not isinstance(raw_plan, list):
                    self._last_plan_error = f"PLANNER_INVALID_SCHEMA: outputs[{index}].plan must be a list"
                    print(f"  ⚠️ Planner 失败: {self._last_plan_error}")
                    return []
                for p in raw_plan:
                    if not isinstance(p, dict):
                        self._last_plan_error = f"PLANNER_INVALID_SCHEMA: outputs[{index}].plan entry must be an object"
                        print(f"  ⚠️ Planner 失败: {self._last_plan_error}")
                        return []
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
            return outputs
        except BudgetExceeded:
            raise
        except Exception as e:
            self._last_plan_error = (
                f"PLANNER_EXCEPTION: {type(e).__name__}: {_response_preview(str(e), 160)}"
            )
            print(f"  ⚠️ Planner 失败: {self._last_plan_error}")

        return []

    def _fallback_plan(self, unit: Unit) -> OutputPlan:
        name = self._guess_filename(unit)
        return OutputPlan(file=name, order=0, type="simple",
                          sources=unit.sources, description="兜底方案")

    def _executor_repair_prompt(self, prompt: str, diagnostic: str) -> str:
        return (
            f"{prompt}\n\n## 上一次响应未通过校验\n{diagnostic[:320]}\n"
            "工具调用参数只属于调用元数据，禁止复制到最终正文。请只输出一个完整 JSON 对象，"
            "不得在对象前后附加其他文本；只保留 code 字段，planned 输出还必须包含 done 布尔字段。"
        )

    def _record_executor_repair(self, file_name: str, step: int, diagnostic: str) -> None:
        checkpoint = getattr(self, "_checkpoint", None)
        if not isinstance(checkpoint, dict):
            return
        checkpoint.setdefault("executor_repairs", []).append({
            "file": file_name,
            "step": step,
            "error": diagnostic[:320] if isinstance(diagnostic, str) else "",
        })
        self._save_checkpoint()

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
            data = _parse_executor_json(result, planned=False)
            if not _valid_code_response(data):
                diagnostic = (f"EXECUTOR_INVALID_JSON: response length={len(result) if isinstance(result, str) else 0} "
                              f"preview={_response_preview(result, 160)}")
                self._record_executor_repair(o.file, 1, diagnostic)
                # 只修复当前输出，避免重新执行整个 Unit。
                result = self.translate_agent.run(self._executor_repair_prompt(prompt, diagnostic))
                data = _parse_executor_json(result, planned=False)
            if _valid_code_response(data):
                print(f"    ✓ {len(data['code'])} 字符")
                return TranslationResult(unit_name="", file_name=o.file, code=data["code"], success=True,
                                         status="generated", completed_steps=1, total_steps=1)
            else:
                return TranslationResult(unit_name="", file_name=o.file,
                                         error=(f"EXECUTOR_INVALID_JSON: response length={len(result) if isinstance(result, str) else 0} "
                                                f"preview={_response_preview(result, 160)}"))
        except BudgetExceeded:
            raise
        except Exception as e:
            return TranslationResult(unit_name="", file_name=o.file,
                                     error=f"EXECUTOR_EXCEPTION: {type(e).__name__}: {_response_preview(str(e), 160)}")

    # ---- Planned 翻译 ----

    def _translate_planned(self, o: OutputPlan, context: str) -> TranslationResult:
        if not o.plan:
            return self._translate_simple(o, context)

        print(f"  {o.file} (planned, {len(o.plan)} 步)")
        accumulated = ""
        total = len(o.plan)
        checkpoint = getattr(self, "_checkpoint", {}).get("steps", {}).get(o.file, {})
        completed = 0
        if (isinstance(checkpoint, dict) and checkpoint.get("total_steps") == total
                and isinstance(checkpoint.get("completed_steps"), int)
                and 0 < checkpoint["completed_steps"] < total
                and isinstance(checkpoint.get("code"), str)
                and checkpoint.get("sha256") == content_hash(checkpoint["code"])):
            completed = checkpoint["completed_steps"]
            accumulated = checkpoint["code"]

        for i, p in enumerate(o.plan):
            check_budget()
            if i < completed:
                continue
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
                context=context,
                resource_hints=self._resource_hints(),
                accumulated=accumulated if accumulated else "无（第一步，请从 import 开始输出完整结构）",
            )

            try:
                # 先用 invoke（无工具），源码已组装好
                result = self.llm.invoke([{"role": "user", "content": prompt}])
                data = _parse_executor_json(result, planned=True)
                if not _valid_code_response(data, planned=True):
                    diagnostic = (f"EXECUTOR_INVALID_JSON: step={i + 1} "
                                  f"response length={len(result) if isinstance(result, str) else 0} "
                                  f"preview={_response_preview(result, 160)}")
                    self._record_executor_repair(o.file, i + 1, diagnostic)
                    # 修复当前步骤，保留已完成步骤和累积代码。
                    result = self.translate_agent.run(self._executor_repair_prompt(prompt, diagnostic))
                    data = _parse_executor_json(result, planned=True)
                if _valid_code_response(data, planned=True):
                    if i == total - 1 and data["done"] is not True:
                        diagnostic = "FINAL_STEP_INCOMPLETE: final response must declare done=true"
                        repaired = None
                        for attempt in range(FINAL_COMPLETION_REPAIR_ATTEMPTS):
                            self._record_executor_repair(o.file, i + 1, diagnostic)
                            repair_prompt = self._executor_repair_prompt(prompt, diagnostic)
                            repair_prompt += (
                                f"\n这是最后一步完成标志修复，第 {attempt + 1}/"
                                f"{FINAL_COMPLETION_REPAIR_ATTEMPTS} 轮。"
                                "请保留下面候选代码，并将 done 设置为 true。\n"
                                f"候选代码:\n{data['code']}"
                            )
                            try:
                                repaired = _parse_executor_json(
                                    self.translate_agent.run(repair_prompt), planned=True)
                            except (BudgetExceeded, KeyboardInterrupt):
                                raise
                            except Exception as exc:
                                diagnostic = f"FINAL_STEP_REPAIR_EXCEPTION: {type(exc).__name__}: {_response_preview(str(exc), 160)}"
                                continue
                            if _valid_code_response(repaired, planned=True) and repaired.get("done") is True:
                                data = repaired
                                break
                            diagnostic = "FINAL_STEP_INCOMPLETE: repair response still requires done=true"
                        else:
                            return TranslationResult(file_name=o.file, code=accumulated,
                                                     error="Final step did not declare completion",
                                                     completed_steps=i, total_steps=total)
                    accumulated = data["code"]
                    # done 只在最后一步生效：LLM 提前宣布完成会把剩余步骤
                    # （如事件处理/业务逻辑）整个跳过，产出只有 UI 没有功能的页面
                    if data.get("done") and i < total - 1:
                        print(f"      ⚠️ LLM 在第 {i + 1}/{total} 步提前返回 done，忽略并继续执行剩余步骤")
                    if getattr(self, "_checkpoint", None) is not None:
                        self._checkpoint.setdefault("steps", {})[o.file] = {
                            "code": accumulated, "sha256": content_hash(accumulated),
                            "completed_steps": i + 1, "total_steps": total, "status": "candidate"}
                        self._save_checkpoint()
                else:
                    return TranslationResult(file_name=o.file, code=accumulated,
                                             error=(f"EXECUTOR_INVALID_JSON: step={i + 1} "
                                                    f"response length={len(result) if isinstance(result, str) else 0} "
                                                    f"preview={_response_preview(result, 160)}"),
                                             completed_steps=i, total_steps=total)
            except BudgetExceeded:
                raise
            except Exception as e:
                return TranslationResult(file_name=o.file, code=accumulated,
                                         error=f"EXECUTOR_EXCEPTION: step={i + 1} {type(e).__name__}: {_response_preview(str(e), 160)}",
                                         completed_steps=i, total_steps=total)

        if accumulated:
            print(f"    ✓ {len(accumulated)} 字符")
            return TranslationResult(unit_name="", file_name=o.file, code=accumulated, success=True,
                                     status="generated", completed_steps=total, total_steps=total)
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
        # Preserve all declarations/signatures.  Truncating the first bytes often
        # discarded the very APIs downstream translations needed to import.
        return '\n'.join(line for line in lines
                         if line.strip().startswith(('import ', 'export ', '@Component',
                                                      '@Entry', 'struct ', '@Prop',
                                                      '@Link', '@State', '@Builder'))
                         or re.match(r'^\s*(?:(?:public|protected|static|async)\s+)*[A-Za-z_$][\w$]*\s*\([^;]*\)\s*(?::[^=]+)?\s*\{?\s*$', line))

    def _write(self, filename: str, code: str):
        ets_dir = self.harmony_root / "entry/src/main/ets/pages"
        ets_dir.mkdir(parents=True, exist_ok=True)
        output = project_path(ets_dir, filename)
        if output.suffix != ".ets":
            raise ArtifactContractError(f"Expected .ets output: {filename}")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(code, encoding='utf-8', newline="")
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
        self.llm = create_pipeline_llm()
        self.tools = ToolRegistry(project_root, summaries, resource_mapping_path)
        self.translator = UnitTranslator(self.llm, self.tools, harmony_root, summaries)
        self.summaries = summaries

    def run(self, layers: list[list[Unit]], deps: dict[str, set[str]]) -> list[TranslationResult]:
        cache: dict[str, str] = {}
        statuses: dict[str, bool] = {}
        try:
            retries = max(0, int(os.getenv("TRANSLATION_UNIT_RETRIES", "1")))
        except ValueError:
            retries = 1
        retryable_errors = (
            "transient", "PLANNER_", "EXECUTOR_", "Final step did not declare completion",
            "plan_validation_failed", "planner_failed",
        )
        units = [unit for layer in layers for unit in layer]
        units, deps = merge_cyclic_units(units, deps)
        layers, _ = topological_sort(units, deps)
        outputs_by_unit: dict[str, dict[str, str]] = {}
        all_results = []
        for depth, layer in enumerate(layers):
            print(f"\n{'=' * 60}")
            print(f"第 {depth} 层 ({len(layer)} 个 unit)")
            print(f"{'=' * 60}")

            for unit in layer:
                check_budget()
                missing = [name for name in deps.get(unit.name, set())
                           if not statuses.get(name, False)]
                if missing:
                    unit_results = [TranslationResult(
                        unit_name=unit.name, status="blocked_dependency",
                        error=f"Incomplete dependencies: {sorted(missing)}")]
                else:
                    dep_ctx = self._dep_context(unit, deps, cache)
                    available = {}
                    for name in sorted(deps.get(unit.name, set())):
                        for filename, api in outputs_by_unit[name].items():
                            if filename in available and available[filename] != api:
                                raise ArtifactContractError(f"Ambiguous dependency output: {filename}")
                            available[filename] = api
                    unit_results = []
                    for attempt in range(retries + 1):
                        unit_results = self.translator.translate(unit, dep_ctx, available)
                        if unit_results and all(result.success for result in unit_results):
                            break
                        if (not any(any(marker in result.error for marker in retryable_errors)
                                    for result in unit_results)
                                or attempt >= retries):
                            break
                        print(f"  ↻ Unit 翻译失败，重试 {attempt + 1}/{retries}: {unit.name}")
                unit_results = unit_results or [TranslationResult(
                    unit_name=unit.name, status="failed", error="Unit produced no result")]
                statuses[unit.name] = all(result.success for result in unit_results)
                all_results.extend(unit_results)
                if statuses[unit.name]:
                    codes = [f"## {r.file_name}\n{self.translator._api_summary(r.code)}"
                             for r in unit_results]
                    cache[unit.name] = "\n".join(codes)
                    outputs_by_unit[unit.name] = {
                        r.file_name: self.translator._api_summary(r.code) for r in unit_results}

        from pipeline.resource_migrator import parse_manifest
        android_root = Path(self.project_root)
        manifest_path = next((android_root / relative for relative in
                              ("app/src/main/AndroidManifest.xml", "src/main/AndroidManifest.xml", "AndroidManifest.xml")
                              if (android_root / relative).is_file()), android_root / "AndroidManifest.xml")
        manifest = build_translation_manifest(android_root, units, all_results, self.summaries,
                                              parse_manifest(str(manifest_path)))
        write_artifact_manifest(self.translator.harmony_root, manifest)
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
                raise ArtifactContractError(f"Dependency has no generated API: {name}")
        return "\n".join(parts)


# ============================================================
# 工具函数
# ============================================================

def _parse_json(text: str) -> dict | None:
    if not isinstance(text, str) or not text.strip():
        return None
    text = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.I)
    if fenced:
        text = fenced.group(1)
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except (ValueError, TypeError):
        return None


def _parse_executor_json(text: str, *, planned: bool = False) -> dict | None:
    """Parse an executor response, including a known tool-response quirk.

    Some OpenAI-compatible gateways concatenate the JSON arguments for a
    tool call with the assistant's final JSON response.  The normal parser
    must remain strict, but the executor can recover this specific shape when
    every preceding JSON object is recognizable as one of our read-only tool
    argument payloads and the final object is a valid code response.
    """
    data = _parse_json(text)
    if _valid_code_response(data, planned=planned):
        return data
    if not isinstance(text, str) or not text.strip():
        return None

    payload = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", payload, re.I)
    if fenced:
        payload = fenced.group(1).strip()

    decoder = json.JSONDecoder()
    values: list[object] = []
    offset = 0
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

    if len(values) < 2 or not isinstance(values[-1], dict):
        return None
    if not all(_is_tool_argument_payload(value) for value in values[:-1]):
        return None
    candidate = values[-1]
    return candidate if _valid_code_response(candidate, planned=planned) else None


def _is_tool_argument_payload(value: object) -> bool:
    """Recognize argument objects for the read-only translation tools."""
    if not isinstance(value, dict):
        return False
    keys = set(value)
    if keys == {"file_path"} and isinstance(value.get("file_path"), str):
        return True
    if keys == {"file_path", "start_line", "end_line"}:
        return (isinstance(value.get("file_path"), str)
                and isinstance(value.get("start_line"), int)
                and isinstance(value.get("end_line"), int))
    if keys == {"ref"} and isinstance(value.get("ref"), str):
        return True
    return not keys


def _parse_json_payload(text: str) -> tuple[object, bool]:
    """Decode a model payload while preserving valid-but-wrong JSON types."""
    if not isinstance(text, str) or not text.strip():
        return None, True
    payload = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", payload, re.I)
    if fenced:
        payload = fenced.group(1)
    try:
        return json.loads(payload), False
    except (ValueError, TypeError):
        return None, True


def _response_preview(text: object, limit: int = 240) -> str:
    """Keep planner diagnostics useful without dumping a full model response."""
    if text is None:
        return "<empty>"
    preview = " ".join(str(text).split())
    if len(preview) > limit:
        return preview[:limit] + "..."
    return preview


def _valid_code_response(data, planned: bool = False) -> bool:
    return (isinstance(data, dict) and isinstance(data.get("code"), str)
            and bool(data["code"].strip()) and (not planned or isinstance(data.get("done"), bool)))


def _safe_format(template: str, **kwargs) -> str:
    """format interprets the template, never braces inside replacement values."""
    return template.format(**kwargs)


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

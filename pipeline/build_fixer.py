"""构建修复循环 — hvigor 构建 → 错误解析 → LLM 反思修复 → 重新构建验证

设计原则（区别于旧版 HarmonyFeedbackAgent）：
- 成败只以 hvigor 的退出码和重新解析的错误日志为准，LLM 的"无需改进"仅用于提前结束单文件的反思轮
- 每轮修复之后必然跟随一次构建；达到最大修复轮数后额外做一次最终构建，
  保证返回的 remaining_errors 一定来自"修复后"的真实编译结果
- remaining_errors_count 由最终错误列表直接计算，不依赖调用方传入
"""

import re
from pathlib import Path
from typing import Any, Optional

from hello_agents.core.llm import HelloAgentsLLM

from pipeline.project_packager import (
    find_invalid_resource_names,
    find_resource_name_conflicts,
    run_hvigor,
)
from pipeline.agents import create_pipeline_llm


# ============================================================
# 构建日志解析
# ============================================================

ANSI_ESCAPE_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")


class ErrorParser:
    """解析 hvigor 构建日志中的编译错误"""

    @staticmethod
    def parse(stdout: str, stderr: str) -> list[dict[str, Any]]:
        errors: list[dict[str, Any]] = []
        # Hvigor 默认输出 ANSI 颜色控制码，若不移除会打断跨行 ArkTS 错误的正则匹配。
        text = ANSI_ESCAPE_RE.sub("", (stdout or "") + "\n" + (stderr or ""))

        # 模块解析失败：Could not resolve "xxx" from "file"
        r_resolve = re.compile(
            r"Could not resolve\s+\"([^\"]+)\"\s+from\s+\"([^\"]+)\"", re.IGNORECASE
        )
        for m in r_resolve.finditer(text):
            missing, from_file = m.groups()
            errors.append({
                "file": from_file.strip(),
                "line": 1,
                "column": 1,
                "message": f"Could not resolve {missing}",
                "code": "HVIGOR_RESOLVE",
            })

        # HarmonyOS 资源名不含扩展名；foo.png 与 foo.webp 会发生冲突。
        r_resource_conflict = re.compile(
            r"Error Message:\s*Resource\s+'([^']+)'\s+conflict\.\s*"
            r"It is first declared at\s+'([^']+)'\s+and declared again at\s+'([^']+)'",
            re.IGNORECASE,
        )
        for m in r_resource_conflict.finditer(text):
            name, first_file, second_file = m.groups()
            errors.append({
                "file": second_file.strip(),
                "line": 1,
                "column": 1,
                "message": f"Resource '{name}' conflicts with '{first_file.strip()}'",
                "code": "RESOURCE_CONFLICT",
            })

        r_invalid_resource = re.compile(
            r"Error Message:\s*Invalid resource name\s+'([^']+)'\."
            r"[^\r\n]*At file:\s*([^\r\n]+)",
            re.IGNORECASE,
        )
        for m in r_invalid_resource.finditer(text):
            name, file_path = m.groups()
            errors.append({
                "file": file_path.strip(),
                "line": 1,
                "column": 1,
                "message": f"Invalid resource name '{name}'",
                "code": "RESOURCE_INVALID_NAME",
            })

        # ArkTS 错误跨行输出：ERROR: 10505001 ArkTS Compiler Error
        #   Error Message: ... At File: E:/.../MainPage.ets:46:46
        r_arkts = re.compile(
            r"ERROR:\s*(\d+)\s+ArkTS Compiler Error\s*"
            r"Error Message:\s*(.*?)\s*At File:\s*(\S+?):(\d+):(\d+)",
            re.IGNORECASE | re.DOTALL,
        )
        for m in r_arkts.finditer(text):
            code, msg, fp, ln, col = m.groups()
            errors.append({
                "file": fp.strip(), "line": int(ln), "column": int(col),
                "message": " ".join(msg.split()), "code": code,
            })

        # 兜底：无编号头的 Error Message ... At File
        r_msg = re.compile(
            r"Error Message:\s*(.*?)\s*At File:\s*(\S+?):(\d+):(\d+)",
            re.IGNORECASE | re.DOTALL,
        )
        for m in r_msg.finditer(text):
            msg, fp, ln, col = m.groups()
            errors.append({
                "file": fp.strip(), "line": int(ln), "column": int(col),
                "message": " ".join(msg.split()), "code": "ARKTS",
            })

        # TypeScript 风格：file(12,3): error TS2304: xxx
        r_ts = re.compile(
            r"([^\(\n]+)\((\d+),(\d+)\):\s*error\s+(TS\d+):\s*(.+)", re.IGNORECASE
        )
        for m in r_ts.finditer(text):
            fp, ln, col, code, msg = m.groups()
            errors.append({
                "file": fp.strip(), "line": int(ln), "column": int(col),
                "message": msg.strip(), "code": code,
            })

        # entry/...:12:3 - error: xxx
        r_entry = re.compile(r"(entry[^:\n]+):(\d+):(\d+)\s*-\s*error:\s*(.+)", re.IGNORECASE)
        for m in r_entry.finditer(text):
            fp, ln, col, msg = m.groups()
            errors.append({
                "file": fp.strip(), "line": int(ln), "column": int(col),
                "message": msg.strip(), "code": "UNKNOWN",
            })

        unique: list[dict[str, Any]] = []
        seen: set = set()
        for err in errors:
            key = (err["file"], err["line"], err["message"])
            if key not in seen:
                seen.add(key)
                unique.append(err)
        return unique


# ============================================================
# 提示词
# ============================================================

SYSTEM_PROMPT = """你是HarmonyOS ArkTS代码专家，擅长：
1. 理解ArkTS声明式UI语法和比TypeScript更严格的类型系统
2. 修复Android转HarmonyOS后的编译错误
3. 遵循最小改动原则，保留业务逻辑"""

# 按错误码/关键词附加的修复提示
ERROR_KNOWLEDGE = {
    "arkts-no-any-unknown": "ArkTS禁止any/unknown，改为具体类型；装饰器参数用Object等具体类型",
    "arkts-no-ctor-signatures-funcs": "ArkTS不支持 new (...args)=>T 构造函数类型，用类或接口替代",
    "TS2304": "找不到名称，常见于Android组件未转换（如TextView→Text）或缺少导入",
    "TS2322": "类型不匹配，检查属性类型或回调函数签名",
    "TS2339": "属性不存在，可能是Android API未转换为ArkTS语法",
    "TS2345": "参数类型错误",
    "HVIGOR_RESOLVE": "模块导入路径无法解析，检查import路径与实际文件位置",
    "RESOURCE_CONFLICT": "同一资源目录只能保留一个逻辑名称相同的文件",
    "RESOURCE_INVALID_NAME": "资源名只能包含字母、数字和下划线",
}

INITIAL_PROMPT = """以下ArkTS文件编译失败，请修复所有错误。

文件: {file_path}

错误列表:
{errors}

当前代码:
```typescript
{source_code}
```

要求:
- 只修复错误，不改动无关部分，保留业务逻辑
- 严格遵循ArkTS规范（禁止any/unknown、禁止构造函数类型、禁止对象字面量缺类型等）
- 返回修复后的完整文件，用```typescript包裹
"""

REFLECT_PROMPT = """请作为资深ArkTS代码审查员检查以下修复方案。

原始错误:
{errors}

修复后的代码:
{content}

请评估修复是否完整、是否引入新的ArkTS规范违规（any/unknown、构造函数类型等）。
如果没有问题，只回答"无需改进"；否则具体指出问题。
"""

REFINE_PROMPT = """请根据审查意见改进代码。

原始错误:
{errors}

上一版代码:
```typescript
{last_attempt}
```

审查意见:
{feedback}

返回改进后的完整文件，用```typescript包裹。
"""


# ============================================================
# 修复循环
# ============================================================

class BuildFixLoop:
    """构建-修复-再验证闭环"""

    def __init__(self, llm: Optional[HelloAgentsLLM] = None,
                 max_fix_rounds: int = 3, reflect_rounds: int = 2):
        self.llm = llm or create_pipeline_llm()
        self.max_fix_rounds = max_fix_rounds
        self.reflect_rounds = reflect_rounds

    def run(self, project_dir: str | Path,
            sync_dir: str | Path | None = None) -> dict[str, Any]:
        """对 project_dir 反复构建并修复，直到通过或达到最大修复轮数。

        Args:
            project_dir: 被构建的完整工程（套模板后的输出目录）
            sync_dir: 可选，修复后的文件同步回的目录（生成侧工程），
                      只同步该目录下已存在的同相对路径文件

        Returns:
            {success, builds, initial_errors_count, remaining_errors_count, remaining_errors}
        """
        project_dir = Path(project_dir)
        sync_dir = Path(sync_dir) if sync_dir else None
        initial_count: Optional[int] = None
        errors: list[dict] = []
        builds = 0

        resource_conflicts = find_resource_name_conflicts(
            project_dir / "entry" / "src" / "main" / "resources"
        )
        invalid_resource_names = find_invalid_resource_names(
            project_dir / "entry" / "src" / "main" / "resources"
        )
        if resource_conflicts or invalid_resource_names:
            errors = []
            for conflict in resource_conflicts:
                files = list(conflict["files"])
                errors.append({
                    "file": str(files[-1]),
                    "line": 1,
                    "column": 1,
                    "message": (
                        f"Resource '{conflict['name']}' conflict: "
                        + ", ".join(str(path) for path in files)
                    ),
                    "code": "RESOURCE_CONFLICT",
                })
            for invalid in invalid_resource_names:
                errors.append({
                    "file": str(invalid["file"]),
                    "line": 1,
                    "column": 1,
                    "message": f"Invalid resource name '{invalid['name']}'",
                    "code": "RESOURCE_INVALID_NAME",
                })
            print(f"\n❌ 构建前检查发现 {len(errors)} 个资源问题")
            for error in errors:
                print(f"  - {error['message']}")
            return self._result(False, 0, len(errors), errors)

        # 共 max_fix_rounds 轮修复，每轮前后都有构建：最后一次构建仅做验证
        for round_no in range(self.max_fix_rounds + 1):
            result = run_hvigor(project_dir)
            builds += 1
            errors = ErrorParser.parse(result.stdout, result.stderr)
            build_ok = result.returncode == 0

            if build_ok and not errors:
                print(f"\n✅ 构建通过（第 {builds} 次构建）")
                return self._result(True, builds, initial_count or 0, [])

            if initial_count is None:
                initial_count = len(errors)

            if not errors:
                print("\n⚠️ 构建失败但未解析到任何错误，无法自动修复（打印日志尾部）")
                self._print_log_tail(result)
                break

            print(f"\n❌ 第 {builds} 次构建发现 {len(errors)} 个错误")
            if round_no == self.max_fix_rounds:
                print(f"⚠️ 已达到最大修复轮数 ({self.max_fix_rounds})，停止修复")
                break

            if any(str(error.get("code", "")).startswith("RESOURCE_") for error in errors):
                print("⚠️ 资源问题需要确定性处理，跳过 LLM 代码修复")
                break

            print(f"\n🤖 第 {round_no + 1}/{self.max_fix_rounds} 轮反思修复...")
            self._fix_files(project_dir, sync_dir, errors)

        return self._result(False, builds, initial_count or 0, errors)

    # ---- 单轮修复 ----

    def _fix_files(self, project_dir: Path, sync_dir: Optional[Path],
                   errors: list[dict]):
        by_file: dict[Path, list[dict]] = {}
        for err in errors:
            fp = self._locate(project_dir, err["file"])
            if fp:
                by_file.setdefault(fp, []).append(err)
            else:
                print(f"  ⚠️ 无法定位错误文件，跳过: {err['file']}")

        for fp, file_errors in by_file.items():
            rel = fp.relative_to(project_dir)
            print(f"\n  📄 {rel} ({len(file_errors)} 个错误)")
            try:
                source = fp.read_text(encoding="utf-8")
            except Exception as e:
                print(f"    ❌ 读取失败: {e}")
                continue

            fixed = self._reflection_fix(str(rel), source, file_errors)
            if not fixed or fixed == source:
                print("    ⚠️ 未产生有效修改")
                continue

            reject_reason = self._validate_fixed(source, fixed)
            if reject_reason:
                print(f"    ⚠️ 修复结果校验不通过，已丢弃: {reject_reason}")
                continue

            backup = fp.with_suffix(fp.suffix + ".bak")
            if not backup.exists():
                backup.write_text(source, encoding="utf-8")
            fp.write_text(fixed, encoding="utf-8")
            print("    ✅ 已写入修复")

            if sync_dir:
                sync_target = sync_dir / rel
                if sync_target.exists():
                    sync_target.write_text(fixed, encoding="utf-8")
                    print(f"    ↩ 已同步回生成工程: {sync_target}")

    def _reflection_fix(self, file_path: str, source: str,
                        errors: list[dict]) -> Optional[str]:
        """单文件反思修复：修复 → 审查 → （必要时）改进。真正的验证靠外层重新构建。"""
        errors_text = self._format_errors(errors)
        current = None
        feedback = ""

        for i in range(self.reflect_rounds):
            if i == 0:
                prompt = INITIAL_PROMPT.format(
                    file_path=file_path, errors=errors_text, source_code=source)
            else:
                prompt = REFINE_PROMPT.format(
                    errors=errors_text, last_attempt=current, feedback=feedback)

            response = self._invoke(prompt)
            fixed = self._extract_code(response)
            if not fixed:
                print("    ⚠️ LLM未返回有效代码")
                break
            current = fixed

            feedback = self._invoke(
                REFLECT_PROMPT.format(errors=errors_text, content=current))
            if "无需改进" in feedback or "no need" in feedback.lower():
                break
            print(f"    💡 反思意见: {feedback[:100]}")

        return current

    @staticmethod
    def _validate_fixed(source: str, fixed: str) -> Optional[str]:
        """LLM 修复结果的合法性检查，返回拒绝原因；通过时返回 None。

        防止退化输出（空文件、只剩一行垃圾等）覆盖原始翻译。
        """
        fixed_lines = [ln for ln in fixed.splitlines() if ln.strip()]
        source_lines = [ln for ln in source.splitlines() if ln.strip()]
        if not fixed_lines:
            return "输出为空"
        if len(source_lines) >= 10 and len(fixed_lines) < len(source_lines) * 0.3:
            return (f"行数骤减（{len(source_lines)} → {len(fixed_lines)}），"
                    "疑似截断或退化输出")
        keywords = ("struct", "class", "export", "import", "function",
                    "interface", "enum", "@Entry", "@Component")
        if not any(kw in fixed for kw in keywords):
            return "不含任何 ArkTS 结构关键字"
        return None

    # ---- 辅助 ----

    @staticmethod
    def _print_log_tail(result, lines: int = 30):
        stdout_tail = "\n".join((result.stdout or "").splitlines()[-lines:])
        stderr_tail = "\n".join((result.stderr or "").splitlines()[-lines:])
        if stdout_tail:
            print("=== stdout ===")
            print(stdout_tail)
        if stderr_tail:
            print("=== stderr ===")
            print(stderr_tail)

    def _invoke(self, prompt: str) -> str:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        try:
            return self.llm.invoke(messages) or ""
        except Exception as e:
            print(f"    ⚠️ LLM调用失败: {e}")
            return ""

    @staticmethod
    def _locate(project_dir: Path, file_path: str) -> Optional[Path]:
        """把错误信息里的路径定位到 project_dir 内的真实文件"""
        p = Path(str(file_path).replace("\\", "/"))
        if p.is_absolute() and p.exists():
            try:
                p.relative_to(project_dir)
                return p
            except ValueError:
                return None
        candidate = project_dir / p
        return candidate if candidate.exists() else None

    @staticmethod
    def _extract_code(response: str) -> Optional[str]:
        m = re.search(r"```(?:typescript|ts|ets)?\s*\n(.*?)```", response, re.DOTALL)
        return m.group(1).strip() if m else None

    @staticmethod
    def _format_errors(errors: list[dict]) -> str:
        lines = []
        for i, err in enumerate(errors, 1):
            hint = ""
            probe = f"{err.get('code', '')} {err.get('message', '')}"
            for key, tip in ERROR_KNOWLEDGE.items():
                if key in probe:
                    hint = f"\n   提示: {tip}"
                    break
            lines.append(
                f"{i}. 第{err['line']}行第{err['column']}列 [{err.get('code', '?')}]: "
                f"{err['message']}{hint}"
            )
        return "\n".join(lines)

    @staticmethod
    def _result(success: bool, builds: int, initial_count: int,
                remaining: list[dict]) -> dict[str, Any]:
        return {
            "success": success,
            "builds": builds,
            "initial_errors_count": initial_count,
            "remaining_errors_count": len(remaining),
            "remaining_errors": remaining,
        }

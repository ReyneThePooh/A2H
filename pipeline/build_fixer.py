"""构建修复循环 — hvigor 构建 → 错误解析 → LLM 反思修复 → 重新构建验证

设计原则（区别于旧版 HarmonyFeedbackAgent）：
- 成败只以 hvigor 的退出码和重新解析的错误日志为准，LLM 的"无需改进"仅用于提前结束单文件的反思轮
- 每轮修复之后必然跟随一次构建；达到最大修复轮数后额外做一次最终构建，
  保证返回的 remaining_errors 一定来自"修复后"的真实编译结果
- remaining_errors_count 由最终错误列表直接计算，不依赖调用方传入
"""

import contextlib
import json
import os
import re
from pathlib import Path
from typing import Any, Optional

from hello_agents.core.llm import HelloAgentsLLM

from pipeline.project_packager import (
    find_component_new_violations,
    find_invalid_resource_names,
    find_resource_name_conflicts,
    run_hvigor,
)
from pipeline.agents import create_pipeline_llm
from pipeline.artifacts import (
    content_hash,
    project_path,
    project_source_fingerprint,
    updated_artifact_manifest,
)
from pipeline.file_transaction import FileBatchTransaction, recover_file_transactions


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

        # 兜底：无编号头的 Error Message ... At File。错误头之间不跨段，
        # 避免把多个 ArkTS 错误拼成一个超长假错误。
        r_msg = re.compile(
            r"Error Message:\s*((?:(?!\r?\n\s*(?:\d+\s+)?ERROR\b).)*?)"
            r"\s*At File:\s*(\S+?):(\d+):(\d+)",
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
            if (err["code"] == "ARKTS"
                    and ("ERROR:" in err["message"]
                         or "ArkTS Compiler Error" in err["message"])):
                continue
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
    "arkts-no-types-in-catch": (
        "catch 子句禁止类型标注：写 catch (e) {}，"
        "在函数体内用 const err = e as Error（或 BusinessError）收窄后再访问属性"
    ),
    "arkts-no-any-unknown": (
        "ArkTS禁止any/unknown，改为具体类型；装饰器参数用Object等具体类型。"
        "例外：catch (e) 的参数必须保持无类型标注，这不算违规"
    ),
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

相关本地依赖（只读，用于核对真实接口签名）:
{dependency_context}

要求:
- 只修复错误，不改动无关部分，保留业务逻辑
- 严格遵循ArkTS规范（禁止any/unknown、禁止构造函数类型、禁止对象字面量缺类型等）
- 禁止用 as never 或无关类型的强制断言绕过类型检查；必须按依赖的真实接口修复
- 返回修复后的完整文件，用```typescript包裹
"""

REFLECT_PROMPT = """请作为资深ArkTS代码审查员检查以下修复方案。

原始错误:
{errors}

修复后的代码:
{content}

请评估修复是否完整、是否引入新的ArkTS规范违规（any/unknown、构造函数类型等）。
注意：try-catch 的 catch (e) 参数不带类型标注是 ArkTS 的强制要求（arkts-no-types-in-catch），
这是正确写法，不要建议给 catch 参数补类型。
如果没有问题，只回答"无需改进"；否则具体指出问题。
"""

REFINE_PROMPT = """请根据审查意见改进代码。

原始错误:
{errors}

上一版代码:
```typescript
{last_attempt}
```

相关本地依赖（只读，用于核对真实接口签名）:
{dependency_context}

审查意见:
{feedback}

返回改进后的完整文件，用```typescript包裹。
"""


# ============================================================
# 修复循环
# ============================================================

def behavior_guard(source: str, fixed: str) -> str | None:
    """Conservative signals, not a proof of semantic equivalence.

    Reject newly introduced empty/log-only methods; existing legitimate empty
    lifecycle hooks are unchanged and do not trigger this check.
    """
    pattern = re.compile(
        r"\b(?:async\s+)?([A-Za-z_$][\w$]*)\s*\([^{}]*\)\s*"
        r"(?::\s*[^{}=;]+)?\s*\{([^{}]*)\}", re.MULTILINE)

    def stubs(code: str) -> set[str]:
        found = set()
        from pipeline.artifacts import code_without_comments
        code = code_without_comments(code)
        for m in pattern.finditer(code):
            name, body = m.groups()
            if name in {"if", "for", "while", "switch", "catch", "constructor"}:
                continue
            body = body.strip()
            if (not body or body == "return;"
                    or re.fullmatch(r"(?:console\.\w+\([^;]*\);?\s*)+", body)):
                found.add(name)
        return found

    introduced = stubs(fixed) - stubs(source)
    if introduced:
        return "BEHAVIOR_REMOVED: empty/log-only methods: " + ", ".join(sorted(introduced))
    # An existing event binding disappearing is a diagnostic refusal rather
    # than an opportunity to erase the interaction to pass compilation.
    for marker in ("onClick", "onChange", "onAction"):
        binding = rf"\.{marker}\s*\("
        if len(re.findall(binding, source)) > len(re.findall(binding, fixed)):
            return "BEHAVIOR_REMOVED: " + marker + " bindings removed"
    return None


class BuildFixLoop:
    """构建-修复-再验证闭环"""

    def __init__(self, llm: Optional[HelloAgentsLLM] = None,
                 max_fix_rounds: int = 4, reflect_rounds: int = 2):
        self.llm = llm
        self.max_fix_rounds = max_fix_rounds
        self.reflect_rounds = reflect_rounds

    def run(self, project_dir: str | Path,
            sync_dir: str | Path | None = None, *,
            workspace: str | Path | None = None,
            _project_lock_held: bool = False) -> dict[str, Any]:
        """直接修复指定工程；中断或达到上限后保留当前代码供续跑。"""
        from run_control import FileLock, atomic_json, check_budget
        project_dir = Path(project_dir).resolve()
        sync_root = Path(sync_dir).resolve() if sync_dir else None
        if sync_root == project_dir:
            sync_root = None
        transaction_roots = [project_dir]
        if sync_root is not None:
            transaction_roots.append(sync_root)
        transaction_dir = (
            Path(workspace).resolve() / "build_transactions"
            if workspace is not None
            else project_dir / ".pipeline_transactions" / "build"
        )
        self._transaction_dir = transaction_dir
        self._transaction_roots = transaction_roots
        lock = (
            contextlib.nullcontext()
            if _project_lock_held
            else FileLock(project_dir.parent / f".{project_dir.name}.repair.lock")
        )
        with lock:
            recover_file_transactions(
                transaction_dir,
                owner="build_repair",
                roots=transaction_roots,
                keep_committed=lambda _transaction_id: True,
            )
            check_budget()
            result = self._run_in_place(project_dir, sync_dir=sync_root)
            if result.get("stop_reason") != "NO_PROGRESS_PERSISTED":
                self._refresh_manifests(project_dir, sync_root)
            result["status"] = "BUILD_VERIFIED" if result["success"] else "BUILD_FAILED"
            if not result["success"]:
                proof_path = project_dir / ".pipeline_build.json"
                try:
                    proof = json.loads(proof_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    proof = None
                if isinstance(proof, dict) and proof.get("status") == "success":
                    proof.update(
                        status="failed",
                        stop_reason=result.get(
                            "stop_reason", "BUILD_VALIDATION_FAILED"
                        ),
                    )
                    atomic_json(proof_path, proof)
            if workspace is not None:
                atomic_json(Path(workspace) / "build_result.json", result)
            return result

    def _refresh_manifests(self, project_dir: Path,
                           sync_root: Optional[Path]) -> None:
        """Refresh both manifests in one recoverable transaction."""
        writes = {}
        preconditions = {}
        for root in (project_dir, sync_root):
            if root is None:
                continue
            manifest_path = root / "translation_manifest.json"
            if not manifest_path.is_file():
                continue
            manifest_before = manifest_path.read_bytes()
            manifest = updated_artifact_manifest(root, {})
            preconditions[manifest_path] = content_hash(manifest_before)
            preconditions.update(self._manifest_preconditions(root, manifest, {}))
            writes[manifest_path] = json.dumps(
                manifest, ensure_ascii=False, indent=2, sort_keys=True
            ).encode("utf-8")
        if not writes:
            return
        transaction = FileBatchTransaction(
            journal_dir=self._transaction_dir,
            owner="build_repair",
            roots=self._transaction_roots,
            writes=writes,
            preconditions=preconditions,
        )
        transaction.commit()
        transaction.finalize()

    @staticmethod
    def _manifest_preconditions(project_dir: Path, manifest: dict,
                                replacements: dict[Path, bytes]) -> dict[Path, str | None]:
        replaced = {Path(path).resolve() for path in replacements}
        return {
            path: output["sha256"]
            for output in manifest.get("outputs", [])
            for path in [project_path(project_dir, output["path"])]
            if path not in replaced
        }

    def _run_in_place(self, project_dir: str | Path,
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

        # A resumed pipeline used to spend another full repair budget on the
        # same unchanged source. Keep one tiny failure marker in the run
        # workspace so repeated resumes stop before another model call.
        progress_state = self._progress_state_path()
        source_fingerprint = project_source_fingerprint(project_dir)
        context_fingerprint = self._repair_context_fingerprint()
        previous = self._read_progress_state(progress_state)
        if (previous is not None
                and previous.get("source_fingerprint") == source_fingerprint):
            if previous.get("context_fingerprint") != context_fingerprint:
                previous = None
            previous_errors = previous.get("errors") if previous is not None else None
            if isinstance(previous_errors, list):
                stopped = self._result(
                    False, 0, int(previous.get("initial_errors_count", len(previous_errors))),
                    previous_errors
                )
                stopped["stop_reason"] = "NO_PROGRESS_PERSISTED"
                return stopped

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
            from run_control import atomic_write, check_budget
            check_budget()
            validation_marker = project_dir / ".pipeline_build_validation_pending"
            atomic_write(validation_marker, b"post-build validation pending\n")
            result = run_hvigor(project_dir)
            builds += 1
            errors = ErrorParser.parse(result.stdout, result.stderr)
            build_ok = result.returncode == 0

            if build_ok and not errors:
                # 编译通过 ≠ 运行时安全：ArkUI 语义静态检查
                # （new @Component 编译不报错，但启动即 TypeError 闪退）
                violations = find_component_new_violations(project_dir)
                if not violations:
                    validation_marker.unlink(missing_ok=True)
                    print(f"\n✅ 构建通过（第 {builds} 次构建）")
                    return self._result(True, builds, initial_count or 0, [])
                errors = [{
                    "file": str(v["file"]),
                    "line": int(v["line"]),
                    "column": 1,
                    "message": (
                        f"ArkUI 组件 '{v['component']}' 是 @Component struct，"
                        "禁止用 new 手动实例化（编译可通过，但运行时在框架"
                        "构造器抛 TypeError: undefined is not callable，"
                        "导致启动闪退）。组件只能在 build() 中声明式使用；"
                        "若该 new 调用是 Android Adapter 直译残留的死代码，"
                        "请删除对应字段声明与全部赋值语句。"
                    ),
                    "code": "ARKUI_NEW_COMPONENT",
                } for v in violations]
                self._invalidate_build_proof(
                    project_dir, "ARKUI_STATIC_VALIDATION_FAILED"
                )
                print(f"\n❌ 构建通过，但 ArkUI 静态检查发现 "
                      f"{len(errors)} 处 new @Component 违规")

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
            sources = project_dir / "entry/src/main/ets"
            before = {str(p): p.read_bytes() for p in sources.rglob("*.ets")}
            self._fix_files(project_dir, sync_dir, errors)
            after = {str(p): p.read_bytes() for p in sources.rglob("*.ets")}
            if before == after:
                stopped = self._result(False, builds, initial_count or 0, errors)
                stopped["stop_reason"] = "NO_PROGRESS"
                self._write_progress_state(
                    progress_state, source_fingerprint, context_fingerprint,
                    initial_count or 0, errors
                )
                return stopped

        result = self._result(False, builds, initial_count or 0, errors)
        self._write_progress_state(
            progress_state, project_source_fingerprint(project_dir),
            context_fingerprint, initial_count or 0, errors
        )
        return result

    def _progress_state_path(self) -> Optional[Path]:
        workspace = getattr(self, "_transaction_dir", None)
        if workspace is None:
            return None
        return Path(workspace).parent / "build_progress.json"

    def _repair_context_fingerprint(self) -> str:
        return content_hash(json.dumps({
            "max_fix_rounds": self.max_fix_rounds,
            "reflect_rounds": self.reflect_rounds,
            "model_id": os.getenv("LLM_MODEL_ID", ""),
            "node_home": os.getenv("NODE_HOME", ""),
            "system_prompt": SYSTEM_PROMPT,
            "error_knowledge": ERROR_KNOWLEDGE,
        }, ensure_ascii=False, sort_keys=True))

    @staticmethod
    def _read_progress_state(path: Optional[Path]) -> Optional[dict[str, Any]]:
        if path is None or not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _write_progress_state(path: Optional[Path], source_fingerprint: str,
                              context_fingerprint: str,
                              initial_errors_count: int,
                              errors: list[dict[str, Any]]) -> None:
        if path is None:
            return
        from run_control import atomic_json

        payload = {
            "source_fingerprint": source_fingerprint,
            "context_fingerprint": context_fingerprint,
            "initial_errors_count": initial_errors_count,
            "errors": errors,
        }
        atomic_json(path, payload)

    # ---- 单轮修复 ----

    @staticmethod
    def _invalidate_build_proof(project_dir: Path, reason: str) -> None:
        from run_control import atomic_json

        proof_path = project_dir / ".pipeline_build.json"
        try:
            proof = json.loads(proof_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(proof, dict) and proof.get("status") == "success":
            proof.update(status="failed", stop_reason=reason)
            atomic_json(proof_path, proof)

    # catch 子句类型标注（arkts-no-types-in-catch）是纯语法问题，直接正则移除，
    # 不进 LLM。注意保留 Promise .catch((e: T) => ...) 回调参数标注（合法写法）。
    CATCH_TYPE_RE = re.compile(
        r"(?<![.\w])catch\s*\(\s*([A-Za-z_$][\w$]*)\s*:\s*[^)]+\)"
    )
    CATCH_ERROR_KEYS = (
        "arkts-no-types-in-catch",
        "Catch clause variable type annotation",
    )
    MANUAL_ERROR_MARKERS = ("实际QWeather模块路径",)

    @classmethod
    def _apply_deterministic_rules(
        cls, source: str, errors: list[dict]
    ) -> tuple[str, list[dict]]:
        """先用确定性规则修复可机械处理的错误，返回 (新代码, 剩余错误)。"""
        catch_errors = [
            err for err in errors
            if any(key in str(err.get("message", "")) for key in cls.CATCH_ERROR_KEYS)
        ]
        if not catch_errors:
            return source, errors
        fixed = cls.CATCH_TYPE_RE.sub(r"catch (\1)", source)
        if fixed == source:
            return source, errors
        remaining = [err for err in errors if err not in catch_errors]
        return fixed, remaining

    def _fix_files(self, project_dir: Path, sync_dir: Optional[Path],
                   errors: list[dict]):
        from run_control import atomic_write
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
            if any(marker in str(error.get("message", ""))
                   for error in file_errors for marker in self.MANUAL_ERROR_MARKERS):
                print("    ⚠️ 检测到占位依赖路径，需人工提供真实模块后再修复")
                continue
            try:
                original = fp.read_bytes()
                source = original.decode("utf-8")
            except Exception as e:
                print(f"    ❌ 读取失败: {e}")
                continue

            base, llm_errors = self._apply_deterministic_rules(source, file_errors)
            if base != source:
                fixed_count = len(file_errors) - len(llm_errors)
                print(f"    🔧 确定性修复: 已移除 catch 子句类型标注（{fixed_count} 个错误）")

            fixed = base
            if llm_errors:
                # 错误多时跳过审查轮：整文件重写 + 再审查极易超过网关 120s
                do_reflect = len(llm_errors) <= 5
                if not do_reflect:
                    print(f"    → 错误较多，本轮只做一次修复、跳过审查")
                dependency_context = self._local_dependency_context(fp, project_dir)
                llm_fixed = self._reflection_fix(
                    str(rel), base, llm_errors,
                    dependency_context=dependency_context, do_reflect=do_reflect)
                if llm_fixed:
                    fixed = llm_fixed

            if not fixed or fixed == source:
                print("    ⚠️ 未产生有效修改")
                continue

            reject_reason = self._validate_fixed(source, fixed)
            if reject_reason:
                print(f"    ⚠️ 修复结果校验不通过，已丢弃: {reject_reason}")
                continue

            if fp.read_bytes() != original:
                raise RuntimeError(
                    f"Build repair source changed while proposal was generated: {fp}"
                )

            replacement = fixed.encode("utf-8")
            backup = fp.with_suffix(fp.suffix + ".bak")
            writes = {fp: replacement}
            if not backup.exists():
                writes[backup] = original
            preconditions = {fp: content_hash(original)}

            sync_root = sync_dir.resolve() if sync_dir else None
            sync_target = None
            if sync_root is not None:
                candidate = (sync_root / rel).resolve()
                if candidate.is_file() and candidate.is_relative_to(sync_root):
                    sync_target = candidate
                    sync_original = sync_target.read_bytes()
                    writes[sync_target] = replacement
                    preconditions[sync_target] = content_hash(sync_original)

            manifest_path = project_dir / "translation_manifest.json"
            if manifest_path.is_file():
                manifest_before = manifest_path.read_bytes()
                manifest = updated_artifact_manifest(
                    project_dir, {fp: replacement}
                )
                preconditions[manifest_path] = content_hash(manifest_before)
                preconditions.update(self._manifest_preconditions(
                    project_dir, manifest, {fp: replacement}
                ))
                writes[manifest_path] = json.dumps(
                    manifest, ensure_ascii=False, indent=2, sort_keys=True
                ).encode("utf-8")
            if sync_root is not None and sync_target is not None:
                sync_manifest = sync_root / "translation_manifest.json"
                if sync_manifest.is_file():
                    sync_manifest_before = sync_manifest.read_bytes()
                    manifest = updated_artifact_manifest(
                        sync_root, {sync_target: replacement}
                    )
                    preconditions[sync_manifest] = content_hash(
                        sync_manifest_before
                    )
                    preconditions.update(self._manifest_preconditions(
                        sync_root, manifest, {sync_target: replacement}
                    ))
                    writes[sync_manifest] = json.dumps(
                        manifest, ensure_ascii=False, indent=2, sort_keys=True
                    ).encode("utf-8")

            roots = getattr(self, "_transaction_roots", None)
            if roots is None:
                roots = [project_dir.resolve()]
                if sync_root is not None:
                    roots.append(sync_root)
            transaction = FileBatchTransaction(
                journal_dir=getattr(self, "_transaction_dir", None),
                owner="build_repair",
                roots=roots,
                writes=writes,
                preconditions=preconditions,
                writer=atomic_write,
            )
            transaction.commit()
            transaction.finalize()
            print("    ✅ 已写入修复")
            if sync_target is not None:
                print(f"    ↩ 已同步回生成工程: {sync_target}")

    def _reflection_fix(self, file_path: str, source: str,
                        errors: list[dict], dependency_context: str = "无",
                        do_reflect: bool = True) -> Optional[str]:
        """单文件反思修复：修复 → 审查 → （必要时）改进。真正的验证靠外层重新构建。"""
        errors_text = self._format_errors(errors)
        current = None
        feedback = ""
        rounds = self.reflect_rounds if do_reflect else 1

        for i in range(rounds):
            if i == 0:
                prompt = INITIAL_PROMPT.format(
                    file_path=file_path, errors=errors_text, source_code=source,
                    dependency_context=dependency_context)
            else:
                prompt = REFINE_PROMPT.format(
                    errors=errors_text, last_attempt=current, feedback=feedback,
                    dependency_context=dependency_context)

            response = self._invoke(prompt)
            fixed = self._extract_code(response)
            if not fixed:
                print("    ⚠️ LLM未返回有效代码")
                break
            current = fixed

            if not do_reflect or i == rounds - 1:
                break
            feedback = self._invoke(
                REFLECT_PROMPT.format(errors=errors_text, content=current))
            if "无需改进" in feedback or "no need" in feedback.lower():
                break
            print(f"    💡 反思意见: {feedback[:100]}")

        return current

    @staticmethod
    def _local_dependency_context(source_path: Path, project_dir: Path,
                                  max_chars: int = 40_000) -> str:
        """Read bounded relative-import dependencies for interface evidence."""
        try:
            source = source_path.read_text(encoding="utf-8")
        except OSError:
            return "无"
        blocks = []
        used = 0
        seen: set[Path] = set()
        for spec in re.findall(r"from\s+['\"](\.[^'\"]+)['\"]", source):
            candidate = (source_path.parent / spec).with_suffix(".ets").resolve()
            if (not candidate.is_relative_to(project_dir.resolve()) or not candidate.is_file()):
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            try:
                content = candidate.read_text(encoding="utf-8")
            except OSError:
                continue
            remaining = max_chars - used
            if remaining <= 0:
                break
            content = content[:remaining]
            relative = candidate.relative_to(project_dir).as_posix()
            blocks.append(f"\n### {relative}\n```typescript\n{content}\n```")
            used += len(content)
        return "\n".join(blocks) if blocks else "无"

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
        if reason := behavior_guard(source, fixed):
            return reason
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
            if self.llm is None:
                self.llm = create_pipeline_llm()
            return self.llm.invoke(messages) or ""
        except Exception as e:
            from run_control import BudgetExceeded
            from pipeline.agents import LLMCallError
            if isinstance(e, (BudgetExceeded, LLMCallError)):
                raise
            print(f"    ⚠️ LLM调用失败: {e}")
            return ""

    @staticmethod
    def _locate(project_dir: Path, file_path: str) -> Optional[Path]:
        """把错误信息里的路径定位到 project_dir 内的真实文件"""
        project_dir = project_dir.resolve()
        p = Path(str(file_path).replace("\\", "/"))
        candidate = (project_dir / p).resolve()
        allowed = project_dir / "entry/src/main/ets"
        return (candidate if candidate.is_relative_to(allowed) and candidate.is_file()
                and candidate.suffix == ".ets" else None)

    @staticmethod
    def _extract_code(response: str) -> Optional[str]:
        m = re.search(
            r"```(?:typescript|ts|ets)?[ \t]*\r?\n(.*?)```",
            response,
            re.DOTALL,
        )
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

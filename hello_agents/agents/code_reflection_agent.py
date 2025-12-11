"""HarmonyOS ArkTS语法修复智能体 - 基于Reflection Agent"""

import re
from typing import Optional, List, Dict, Any
from pathlib import Path
from hello_agents.core.agent import Agent
from hello_agents.core.llm import HelloAgentsLLM
from hello_agents.core.config import Config
from datetime import datetime
from dotenv import load_dotenv

# 导入你的检查工具
import sys

sys.path.append(str(Path(__file__).parent.parent.parent))
from auto_check_harmony_project import prepare_work_dir, run_hvigor, WORK_DIR, SOURCE_PROJECT_DIR


class ErrorParser:
    """解析hvigor构建错误日志"""

    @staticmethod
    def parse_build_output(stdout: str, stderr: str) -> List[Dict[str, Any]]:
        errors: List[Dict[str, Any]] = []
        text = (stdout or "") + "\n" + (stderr or "")
        lines = text.splitlines()

        r_resolve = re.compile(r"Could not resolve\s+\"([^\"]+)\"\s+from\s+\"([^\"]+)\"", re.IGNORECASE)
        for line in lines:
            m = r_resolve.search(line)
            if m:
                missing, from_file = m.groups()
                try:
                    errors.append({
                        "file": from_file.strip(),
                        "line": 1,
                        "column": 1,
                        "message": f"Could not resolve {missing}",
                        "code": "HVIGOR_RESOLVE",
                    })
                except Exception:
                    pass

        r_head = re.compile(r"^.*?ERROR:\s*(\d+)\s+ArkTS Compiler Error", re.IGNORECASE)
        r_msg = re.compile(r"Error Message:\s*(.*?)\s*At File:\s*(.+?):(\d+):(\d+)", re.IGNORECASE)
        i = 0
        n = len(lines)
        while i < n:
            m = r_head.search(lines[i])
            if m:
                code = m.group(1)
                j = i + 1
                while j < n and lines[j].strip() == "":
                    j += 1
                if j < n:
                    m2 = r_msg.search(lines[j])
                    if m2:
                        msg, fp, ln, col = m2.groups()
                        try:
                            errors.append({
                                "file": fp.strip(),
                                "line": int(ln),
                                "column": int(col),
                                "message": msg.strip(),
                                "code": code,
                            })
                        except Exception:
                            pass
                i = j + 1
            else:
                i += 1

        r_msg_anywhere = re.compile(r"Error Message:\s*(.*?)\s*At File:\s*(.+?):(\d+):(\d+)", re.IGNORECASE)
        r_code_near = re.compile(r"ERROR:\s*(\d+)\s+ArkTS Compiler Error", re.IGNORECASE)
        for idx, line in enumerate(lines):
            m2 = r_msg_anywhere.search(line)
            if m2:
                msg, fp, ln, col = m2.groups()
                code = None
                for k in range(max(0, idx - 3), idx + 1):
                    mh = r_code_near.search(lines[k])
                    if mh:
                        code = mh.group(1)
                        break
                try:
                    errors.append({
                        "file": fp.strip(),
                        "line": int(ln),
                        "column": int(col),
                        "message": msg.strip(),
                        "code": code or "ARKTS",
                    })
                except Exception:
                    pass

        r_ts = re.compile(r"([^\(]+)\((\d+),(\d+)\):\s*error\s+(TS\d+):\s*(.+)", re.IGNORECASE)
        for m in r_ts.finditer(text):
            try:
                fp, ln, col, code, msg = m.groups()
                errors.append({
                    "file": fp.strip(),
                    "line": int(ln),
                    "column": int(col),
                    "message": msg.strip(),
                    "code": code,
                })
            except Exception:
                pass

        r_entry = re.compile(r"(entry[^:]+):(\d+):(\d+)\s*-\s*error:\s*(.+)", re.IGNORECASE)
        for m in r_entry.finditer(text):
            try:
                fp, ln, col, msg = m.groups()
                errors.append({
                    "file": fp.strip(),
                    "line": int(ln),
                    "column": int(col),
                    "message": msg.strip(),
                    "code": "UNKNOWN",
                })
            except Exception:
                pass

        unique: List[Dict[str, Any]] = []
        seen: set = set()
        for err in errors:
            key = (err.get("file"), err.get("line"), err.get("message"))
            if key not in seen:
                seen.add(key)
                unique.append(err)
        return unique


class HarmonyMemory:
    """扩展Memory类，专门用于HarmonyOS代码修复场景"""

    def __init__(self):
        self.records: List[Dict[str, Any]] = []
        self.error_history: List[List[Dict]] = []  # 每轮的错误列表
        self.fix_count = 0
        self.iteration_history = []  # ← 确保这个存在
        self.total_errors_fixed = 0  # ← 添加这个初始化

    def add_record(self, record_type: str, content: Any):
        """添加记录"""
        self.records.append({"type": record_type, "content": content})

    def add_build_result(self, errors: List[Dict], iteration: int):
        """记录构建结果"""
        self.error_history.append(errors)
        self.add_record("build_check", {
            "iteration": iteration,
            "error_count": len(errors),
            "errors": errors
        })

    def add_fix_attempt(self, file_path: str, fixed_code: str, errors_fixed: int):
        """记录修复尝试"""
        self.fix_count += errors_fixed
        self.add_record("fix_attempt", {
            "file": file_path,
            "code": fixed_code,
            "errors_fixed": errors_fixed
        })

    def get_last_errors(self) -> List[Dict]:
        """获取最近一次的错误列表"""
        return self.error_history[-1] if self.error_history else []

    def get_summary(self) -> str:
        """获取修复摘要"""
        if not self.error_history:
            return "未进行任何检查"

        summary = f"总迭代次数: {len(self.error_history)}\n"
        summary += f"  总修复错误数: {self.fix_count}\n"
        summary += f"  错误变化: {len(self.error_history[0])} → {len(self.error_history[-1])}"
        return summary


# HarmonyOS专用提示词模板
HARMONY_PROMPTS = {
    "initial": """你是HarmonyOS ArkTS代码专家。以下是一个从Android转换而来的ArkTS文件，包含语法错误。

文件: 
{file_path}

错误列表:
{errors}

原始代码:
```typescript
{source_code}
请修复所有语法错误，返回完整的修复后代码。注意：

只修复错误，不改动其他部分
保持原有缩进和风格
使用ArkTS声明式语法
常见转换：TextView→Text, EditText→TextInput, LinearLayout→Column/Row
直接返回修复后的完整代码，用```typescript包裹。
""", "reflect": """请作为资深代码审查员，检查以下ArkTS代码修复方案。
原始错误:
{errors}

修复后的代码:
{content}
请评估：

1.是否所有错误都已修复？
2.是否引入了新问题？
3.代码质量如何（可读性、规范性）？
如果代码完美，回答"无需改进"。
否则，请具体指出问题和改进建议。
""", "refine": """请根据审查意见进一步改进代码。
原始错误:
{errors}

上一版代码:
{last_attempt}
审查意见:
{feedback}

请返回改进后的完整代码，用```typescript包裹。
"""
}


class HarmonyFeedbackAgent(Agent):
    """
    HarmonyOS代码语法修复智能体 - 基于Reflection Agent模式
    继承自基础Agent，使用反思-优化循环来修复代码错误

    工作流程：
    1. 执行hvigor构建检查
    2. 解析编译错误日志
    3. 对每个文件使用反思-优化循环修复
    4. 重新检查直到无错误或达到最大迭代次数
    """

    # ArkTS错误知识库
    ERROR_KNOWLEDGE = {
        "TS2304": "找不到名称，常见于Android组件未转换（如TextView→Text）或缺少导入",
        "TS2322": "类型不匹配，检查属性类型或回调函数签名",
        "TS2339": "属性不存在，可能是Android API未转换为ArkTS声明式语法",
        "TS2741": "缺少必需属性",
        "TS2345": "参数类型错误",
        "TS1005": "语法错误，缺少符号",
        "TS1109": "表达式预期",
    }

    # Android到HarmonyOS映射
    COMPONENT_MAP = {
        "TextView": "Text",
        "Button": "Button",
        "EditText": "TextInput",
        "ImageView": "Image",
        "LinearLayout": "Column/Row",
        "RelativeLayout": "Stack/RelativeContainer",
        "RecyclerView": "List",
        "ViewPager": "Swiper",
        "FrameLayout": "Stack",
        "ScrollView": "Scroll",
        "setText": "Text(content)",
        "setOnClickListener": ".onClick(() => {})",
        "findViewById": "@State变量绑定",
    }

    def __init__(
            self,
            name: str = "HarmonyFeedbackAgent",
            llm: Optional[HelloAgentsLLM] = None,
            system_prompt: Optional[str] = None,
            config: Optional[Config] = None,
            max_iterations: int = 1,
            source_dir: Path = SOURCE_PROJECT_DIR,
            work_dir: Path = WORK_DIR
    ):
        # 系统提示词
        default_system = """你是HarmonyOS ArkTS代码专家，擅长：
        1.理解ArkTS声明式UI语法和TypeScript类型系统
        2.修复Android转HarmonyOS后的语法错误
        3.遵循最小改动原则，保留业务逻辑
        4.编写符合ArkTS官方规范的代码
        5.你的任务是修复代码中的语法错误，使其能够通过编译。
        6.忽略未引用的资源文件错误 
        """
        super().__init__(
            name=name,
            llm=llm,
            system_prompt=system_prompt or default_system,
            config=config
        )

        self.max_iterations = max_iterations
        self.source_dir = Path(source_dir)
        self.work_dir = Path(work_dir)
        self.error_parser = ErrorParser()
        self.memory = HarmonyMemory()
        self.prompts = HARMONY_PROMPTS
        self.iteration_history = []  # 迭代历史记录
        self.total_errors_fixed = 0  # 累计修复的错误数

    def _build_result(self, success: bool, iteration: int, **kwargs) -> Dict[str, Any]:
        """构建最终结果字典

        Args:
            success: 是否成功
            iteration: 迭代次数
            **kwargs: 额外的键值对（如 error, final_errors 等）
        """
        result = {
            'success': success,
            'iterations': iteration,
            'total_errors_fixed': self.total_errors_fixed,
            'history': self.iteration_history,
            'timestamp': datetime.now().isoformat(),
        }

        # 合并额外参数
        result.update(kwargs)

        return result

    def run(self, auto_start: bool = True, **kwargs) -> Dict[str, Any]:
        """
        运行HarmonyOS代码检查与修复流程

        Args:
            auto_start: 是否自动开始检查
            **kwargs: 传递给LLM的其他参数

        Returns:
            {
                "success": bool,
                "iterations": int,
                "total_errors_fixed": int,
                "final_errors": List[Dict],
                "summary": str
            }
        """
        print(f"\n{'=' * 70}")
        print(f"🚀 {self.name} 开始HarmonyOS代码检查与修复")
        print(f"{'=' * 70}")

        iteration = 0

        while iteration < self.max_iterations:
            iteration += 1
            print(f"\n{'─' * 70}")
            print(f"📍 第 {iteration}/{self.max_iterations} 轮检查")
            print(f"{'─' * 70}")

            # 步骤1: 准备工作目录
            print("\n[1/4] 📂 准备检查环境...")
            try:
                prepare_work_dir()
            except Exception as e:
                print(f"❌ 准备失败: {e}")
                return self._build_result(False, iteration, error=str(e))

            # 步骤2: 执行构建检查
            print("\n[2/4] 🔨 执行hvigor构建...")
            result = run_hvigor()

            # 步骤3: 解析错误
            print("\n[3/4] 🔍 解析构建日志...")
            errors = self.error_parser.parse_build_output(result.stdout, result.stderr)
            self.memory.add_build_result(errors, iteration)

            build_failed = getattr(result, 'returncode', 1) != 0

            if not errors and not build_failed:
                print(f"\n{'=' * 70}")
                print("✅ 构建成功！所有语法错误已修复。")
                print(f"{'=' * 70}")
                return self._build_result(True, iteration)

            print(f"\n❌ 发现 {len(errors)} 个错误:")
            formatted_errors = self._format_errors(errors)  # ✅ 正确
            print(formatted_errors)

            # 步骤4: 使用Reflection模式修复
            print(f"\n[4/4] 🤖 使用反思-优化模式修复...")
            self._fix_with_reflection(errors, **kwargs)

        # 达到最大迭代次数
        print(f"\n{'=' * 70}")
        print(f"⚠️ 已达到最大迭代次数 ({self.max_iterations})")
        print(f"{'=' * 70}")

        final_errors = self.memory.get_last_errors()
        return self._build_result(False, iteration, final_errors=final_errors)

    def _fix_with_reflection(self, errors: List[Dict], **kwargs):
        """使用反思-优化循环修复代码"""
        # 按文件分组错误
        errors_by_file = {}
        for err in errors:
            file_path = err.get('file', '')
            if file_path:
                clean_path = self._clean_file_path(file_path)
                if clean_path not in errors_by_file:
                    errors_by_file[clean_path] = []
                errors_by_file[clean_path].append(err)

        # 逐文件修复
        for clean_path, file_errors in errors_by_file.items():
            print(f"\n  📄 处理文件: {clean_path} ({len(file_errors)} 个错误)")

            source_file = self.source_dir / "entry" / "src" / "main" / "ets" / clean_path
            if not source_file.exists():
                print(f"  ⚠️ 文件不存在，跳过")
                continue

            try:
                with open(source_file, 'r', encoding='utf-8') as f:
                    source_code = f.read()
            except Exception as e:
                print(f"  ❌ 读取失败: {e}")
                continue

            # 使用Reflection模式修复单个文件
            fixed_code = self._reflection_fix_single_file(
                clean_path,
                source_code,
                file_errors,
                **kwargs
            )

            if fixed_code and fixed_code != source_code:
                # 保存修复后的代码
                try:
                    with open(source_file, 'w', encoding='utf-8') as f:
                        f.write(fixed_code)
                    print(f"  ✅ 已保存修复")
                    self.memory.add_fix_attempt(clean_path, fixed_code, len(file_errors))
                except Exception as e:
                    print(f"  ❌ 保存失败: {e}")

    def _reflection_fix_single_file(
            self,
            file_path: str,
            source_code: str,
            errors: List[Dict],
            max_file_iterations: int = 2,
            **kwargs
    ) -> Optional[str]:
        """
        对单个文件使用反思-优化循环

        Args:
            file_path: 文件路径
            source_code: 源代码
            errors: 错误列表
            max_file_iterations: 单文件最大迭代次数
            **kwargs: 传递给LLM的参数

        Returns:
            修复后的代码
        """
        print(f"\n    🔄 开始反思-优化循环 (最多{max_file_iterations}轮)")

        current_code = source_code
        last_feedback = ""

        for i in range(max_file_iterations):
            print(f"\n    ── 第 {i + 1}/{max_file_iterations} 轮 ──")

            # 1️⃣ 初始修复/优化尝试
            print(f"    ➤ {'初始修复' if i == 0 else '优化修复'}...")

            if i == 0:
                # 第一轮：初始修复
                prompt = self.prompts["initial"].format(
                    file_path=file_path,
                    errors=self._format_errors(errors),
                    source_code=current_code
                )
            else:
                # 后续轮：基于反馈优化
                prompt = self.prompts["refine"].format(
                    errors=self._format_errors(errors),
                    last_attempt=current_code,
                    feedback=last_feedback
                )

            fixed_code = self._extract_code_from_response(
                self._get_llm_response(prompt, **kwargs)
            )

            if not fixed_code:
                print(f"    ⚠️ LLM未返回有效代码")
                break

            # 2️⃣ 反思检查
            print(f"    ➤ 反思检查...")
            reflect_prompt = self.prompts["reflect"].format(
                errors=self._format_errors(errors),
                content=fixed_code
            )

            feedback = self._get_llm_response(reflect_prompt, **kwargs)
            last_feedback = feedback

            # 3️⃣ 判断是否需要继续
            if "无需改进" in feedback or "no need" in feedback.lower():
                print(f"    ✅ 反思认为代码已优化完成")
                return fixed_code

            print(f"    💡 反思意见: {feedback[:100]}...")
            current_code = fixed_code

        # 返回最后一次的修复结果
        return current_code

    def _get_llm_response(self, prompt: str, **kwargs) -> str:
        """调用LLM获取响应"""
        try:
            print(
                f"[LLM] provider={getattr(self.llm, 'provider', None)}, model={getattr(self.llm, 'model', None)}, base_url={getattr(self.llm, 'base_url', None)}")
        except Exception:
            pass
        messages = [{"role": "user", "content": prompt}]
        if self.system_prompt:
            messages.insert(0, {"role": "system", "content": self.system_prompt})
        return self.llm.invoke(messages, **kwargs) or ""

    def _extract_code_from_response(self, response: str) -> Optional[str]:
        """从LLM响应中提取代码"""
        # 尝试提取代码块
        code_match = re.search(r'```(?:typescript|ts|ets)?\s*\n(.*?)```', response, re.DOTALL)
        if code_match:
            return code_match.group(1).strip()

        # 如果没有代码块，尝试返回整个响应（去除注释行）
        lines = response.split('\n')
        code_lines = [line for line in lines if not line.strip().startswith('//') and line.strip()]
        if code_lines:
            return '\n'.join(code_lines)

        return None

    def _format_errors(self, errors: List[Dict]) -> str:
        """格式化错误列表为可读文本"""
        formatted = []
        for i, err in enumerate(errors, 1):
            code = err.get('code', 'UNKNOWN')
            knowledge = self.ERROR_KNOWLEDGE.get(code, "未知错误类型")

            formatted.append(f"""
        错误 #{i}:
        文件: {err.get('file')}
        位置: 第{err.get('line')}行, 第{err.get('column')}列
        错误码: {code}
        信息: {err.get('message')}
        提示: {knowledge}
""")
        return '\n'.join(formatted)

    def _clean_file_path(self, file_path: str) -> str:
        p = str(file_path).replace('\\', '/')
        anchors = ['entry/src/main/ets/', '/entry/src/main/ets/']
        for a in anchors:
            if a in p:
                p = p.split(a, 1)[1]
                break
        p = p.lstrip('/')
        return p

    def _print_errors(self, errors: List[Dict]) -> None:
        print(self._format_errors(errors))


if __name__ == "__main__":
    load_dotenv()
    config = Config.from_env()
    llm = HelloAgentsLLM()

    agent = HarmonyFeedbackAgent(
        llm=llm,
        config=config,
        max_iterations=3
    )

    result = agent.run()

    print("\n" + "=" * 70)
    print("🎯 最终结果")
    print("=" * 70)

    if result.get('success'):
        print("🎉 所有错误已修复！")
        print(f"总共修复了 {result.get('total_errors_fixed')} 个错误")
        print(f"迭代次数: {result.get('iterations')}")
    else:
        print(f"⚠️ 仍有 {result.get('remaining_errors_count', 0)} 个错误未解决")
        print("建议：检查剩余错误，可能需要人工介入")
        if result.get('final_errors'):
            print("\n剩余错误:")
            for i, err in enumerate(result['final_errors'][:3], 1):
                print(f"  {i}. {err.get('file')} L{err.get('line')}: {err.get('message')}")


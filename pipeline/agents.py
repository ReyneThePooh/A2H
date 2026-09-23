"""流水线专职 Agent — 基于 HelloAgents 框架的 FunctionCallAgent

将翻译流水线中需要 LLM + 工具调用的职责封装为 hello_agents Agent 子类：
- UnitBuildAgent        — 翻译单元划分
- DependencyReviewAgent — Unit 依赖审核与补充
- TranslationPlanAgent  — 翻译方案规划（Planner）
- UnitTranslateAgent    — 带工具的翻译回退（Executor）

同时提供：
- create_pipeline_llm     — 用流水线的 LLM_* 环境变量构造 HelloAgentsLLM
- ToolRegistryAdapter     — 把 analyzers.tools.ToolRegistry 适配成
  FunctionCallAgent 期望的工具注册表接口
"""

import os
import json
import time
from dataclasses import dataclass
from typing import Any, Optional

from hello_agents.core.exceptions import HelloAgentsException
from hello_agents.core.llm import HelloAgentsLLM
from hello_agents.agents.function_call_agent import FunctionCallAgent
from run_control import BudgetExceeded, check_budget, consume_budget, remaining_timeout


# ============================================================
# 瞬态错误自动重试
# ============================================================

#: 判定为"可重试的瞬态错误"的关键词（匹配异常消息，忽略大小写）
_MAX_ATTEMPTS = 3        # 总尝试次数（首次 + 2 次重试）
_BASE_DELAY_S = 2.0      # 指数退避基数：2s → 4s


class LLMCallError(HelloAgentsException):
    """Typed, redacted transport/protocol failure; never a NO_CHANGE result."""
    def __init__(self, reason, *, retryable=False, status_code=None):
        self.reason = reason
        self.retryable = retryable
        self.status_code = status_code
        super().__init__(f"LLM {reason}" + (f" (HTTP {status_code})" if status_code else ""))


class IncompleteResponseError(LLMCallError):
    def __init__(self, reason="incomplete_response", *, retryable=True):
        super().__init__(reason, retryable=retryable)


def _classify_llm_error(exc):
    if isinstance(exc, (BudgetExceeded, LLMCallError)):
        return exc
    status = getattr(exc, "status_code", None)
    names = {kind.__name__ for kind in type(exc).__mro__}
    if status in (401, 403):
        return LLMCallError("authentication", status_code=status)
    if status == 429 or (isinstance(status, int) and status >= 500):
        return LLMCallError("transient_http", retryable=True, status_code=status)
    message = str(exc).lower()
    if (names & {"APIError"} and any(marker in message for marker in
            ("stream was interrupted", "response stream was interrupted", "connection reset"))):
        return LLMCallError("transport", retryable=True)
    if names & {"TimeoutError", "APITimeoutError", "ReadTimeout", "ConnectTimeout",
                "APIConnectionError", "ConnectionError", "ConnectError", "ReadError",
                "RemoteProtocolError"}:
        return LLMCallError("transport", retryable=True)
    # Some OpenAI-compatible gateways drop the response before the SDK can
    # attach a status code. Retry those ambiguous failures, but do not retry
    # an explicit client-side 4xx error such as an invalid request.
    return LLMCallError("request_failed", retryable=status is None, status_code=status)


def _is_transient(exc: Exception) -> bool:
    return bool(getattr(_classify_llm_error(exc), "retryable", False))


def _retry_transient(fn, what: str):
    """执行 fn()；瞬态网络错误指数退避重试，非瞬态错误（如鉴权失败）立即抛出。"""
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        check_budget()
        try:
            return fn()
        except BudgetExceeded:
            raise
        except Exception as e:
            failure = _classify_llm_error(e)
            if attempt == _MAX_ATTEMPTS or not failure.retryable:
                if failure is e:
                    raise
                raise failure from e
            delay = _BASE_DELAY_S * (2 ** (attempt - 1))
            print(f"      ⏳ {what}瞬态错误，{delay:.0f}s 后重试"
                  f"（{attempt}/{_MAX_ATTEMPTS - 1}）: {failure.reason}")
            time.sleep(remaining_timeout(delay))
            check_budget()


def _request_client(llm, timeout):
    # Disable SDK retries so every network attempt is visible to our budget.
    client = llm._client
    return client.with_options(max_retries=0, timeout=timeout) if hasattr(client, "with_options") else client


class RetryingLLM(HelloAgentsLLM):
    """invoke 自动重试瞬态错误的 HelloAgentsLLM。

    默认走流式拉取再拼成完整字符串：Cloudflare 524 的触发条件是
    「120 秒内未返回完整响应」；流式会边生成边回传，长修复不再被掐断。
    网关抖动（Connection error / 限流 / 5xx）仍做指数退避重试。
    """

    def invoke(self, messages: list[dict[str, str]], **kwargs) -> str:
        try:
            return _retry_transient(
                lambda: self._invoke_stream(messages, **kwargs),
                "LLM 调用",
            )
        except IncompleteResponseError as exc:
            if exc.reason != "finish_missing":
                raise
            # Some OpenAI-compatible gateways omit the terminal chunk in
            # streaming mode. A validated non-stream response is safer than
            # accepting possibly truncated streamed source code.
            print("      ⚠️ 流式响应缺少结束标记，改用非流式请求兜底", flush=True)
            return _retry_transient(
                lambda: self._invoke_nonstream(messages, **kwargs),
                "LLM 非流式兜底",
            )

    def _invoke_stream(self, messages: list[dict[str, str]], **kwargs) -> str:
        consume_budget("llm_calls")
        timeout = remaining_timeout(float(kwargs.get("timeout", self.timeout)))
        response = None
        try:
            options = dict(kwargs)
            options.pop("timeout", None)
            options.pop("stream", None)
            options.setdefault("temperature", self.temperature)
            options.setdefault("max_tokens", self.max_tokens)
            response = _request_client(self, timeout).chat.completions.create(
                model=self.model,
                messages=messages,
                stream=True,
                timeout=timeout,
                **options,
            )
            parts: list[str] = []
            finish_reason = None
            for chunk in response:
                check_budget()
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                delta = getattr(choices[0], "delta", None)
                text = getattr(delta, "content", None) if delta else None
                if text:
                    parts.append(text)
                if getattr(delta, "refusal", None):
                    raise LLMCallError("refusal")
                reason = getattr(choices[0], "finish_reason", None)
                if reason:
                    finish_reason = reason
            check_budget()
            if finish_reason == "content_filter":
                raise LLMCallError("content_filter")
            if finish_reason != "stop":
                reason = f"finish_{finish_reason or 'missing'}"
                # A cleanly closed stream without its terminal event is often
                # a gateway protocol incompatibility. Let invoke() switch to
                # its validated non-stream fallback immediately.
                raise IncompleteResponseError(reason, retryable=reason != "finish_missing")
            result = "".join(parts)
            if not result.strip():
                raise IncompleteResponseError("empty_response")
            return result
        except (BudgetExceeded, LLMCallError):
            raise
        except Exception as e:
            raise _classify_llm_error(e) from e
        finally:
            if response is not None and hasattr(response, "close"):
                try:
                    response.close()
                except Exception:
                    pass  # Cleanup must not hide a typed failure or budget stop.

    def _invoke_nonstream(self, messages: list[dict[str, str]], **kwargs) -> str:
        consume_budget("llm_calls")
        timeout = remaining_timeout(float(kwargs.get("timeout", self.timeout)))
        try:
            options = dict(kwargs)
            options.pop("timeout", None)
            options.pop("stream", None)
            options.setdefault("temperature", self.temperature)
            options.setdefault("max_tokens", self.max_tokens)
            response = _request_client(self, timeout).chat.completions.create(
                model=self.model,
                messages=messages,
                stream=False,
                timeout=timeout,
                **options,
            )
            check_budget()
            if isinstance(response, str):
                lowered = response.lstrip().lower()
                if lowered.startswith("<!doctype html") or lowered.startswith("<html"):
                    raise LLMCallError("gateway_html_response")
                raise LLMCallError("invalid_response_format")
            choices = getattr(response, "choices", None) or []
            if not choices:
                raise IncompleteResponseError("empty_choices")
            choice = choices[0]
            finish_reason = getattr(choice, "finish_reason", None)
            if finish_reason == "content_filter":
                raise LLMCallError("content_filter")
            if finish_reason != "stop":
                raise IncompleteResponseError(f"finish_{finish_reason or 'missing'}")
            content = getattr(getattr(choice, "message", None), "content", None)
            if isinstance(content, list):
                parts = []
                for item in content:
                    text = item.get("text") if isinstance(item, dict) else getattr(item, "text", None)
                    if text:
                        parts.append(text)
                content = "".join(parts)
            result = content if isinstance(content, str) else (str(content) if content is not None else "")
            if not result.strip():
                raise IncompleteResponseError("empty_response")
            return result
        except (BudgetExceeded, LLMCallError):
            raise
        except Exception as e:
            raise _classify_llm_error(e) from e


# ============================================================
# LLM 工厂
# ============================================================

def create_pipeline_llm(temperature: float = 0.3) -> HelloAgentsLLM:
    """构造流水线使用的 HelloAgentsLLM（带瞬态错误重试）。

    HelloAgentsLLM 与原 LLMClient 读取相同的环境变量
    （LLM_MODEL_ID / LLM_API_KEY / LLM_BASE_URL / LLM_TIMEOUT），
    仅超时默认值不同，这里显式保持流水线原默认 180 秒。
    """
    return RetryingLLM(
        temperature=temperature,
        timeout=int(os.getenv("LLM_TIMEOUT", "180")),
    )


# ============================================================
# 工具注册表适配器
# ============================================================

@dataclass
class _AdapterParam:
    """FunctionCallAgent 期望的参数描述对象"""
    name: str
    type: str
    description: str = ""
    default: Any = None
    required: bool = True


class _AdapterTool:
    """把一条 OpenAI function schema 包装成 FunctionCallAgent 期望的 Tool 对象"""

    def __init__(self, schema: dict, executor):
        fn = schema["function"]
        self.name = fn["name"]
        self.description = fn.get("description", "")
        self._executor = executor

        params_schema = fn.get("parameters", {})
        required = set(params_schema.get("required", []))
        self._params = [
            _AdapterParam(
                name=pname,
                type=p.get("type", "string"),
                description=p.get("description", ""),
                required=pname in required,
            )
            for pname, p in params_schema.get("properties", {}).items()
        ]

    def get_parameters(self) -> list[_AdapterParam]:
        return self._params

    def run(self, parameters: dict) -> str:
        return self._executor(self.name, parameters)


class ToolRegistryAdapter:
    """analyzers.tools.ToolRegistry → FunctionCallAgent 工具注册表接口

    FunctionCallAgent 对注册表是鸭子类型访问，只需要
    get_all_tools / get_tool / get_function / get_tools_description。
    """

    def __init__(self, pipeline_tools):
        self._tools = [
            _AdapterTool(schema, pipeline_tools.execute)
            for schema in pipeline_tools.get_tool_schemas()
        ]
        self._by_name = {t.name: t for t in self._tools}

    def get_all_tools(self) -> list[_AdapterTool]:
        return list(self._tools)

    def get_tool(self, name: str) -> Optional[_AdapterTool]:
        return self._by_name.get(name)

    def get_function(self, name: str):
        return None

    def get_tools_description(self) -> str:
        # 各 Agent 的系统提示词已自带工具说明，返回空避免重复注入
        return ""


# ============================================================
# 流水线 Agent 基类
# ============================================================

class PipelineAgent(FunctionCallAgent):
    """无状态的函数调用 Agent：每次 run 前清空历史。

    流水线的每次调用都是独立任务，不应携带上一次调用的对话历史。
    """

    def __init__(self, name: str, llm: HelloAgentsLLM, tools,
                 system_prompt: str, max_tool_iterations: int = 6):
        super().__init__(
            name=name,
            llm=llm,
            system_prompt=system_prompt,
            tool_registry=ToolRegistryAdapter(tools),
            max_tool_iterations=max_tool_iterations,
        )

    def run(self, input_text: str, **kwargs) -> str:
        self.clear_history()
        return super().run(input_text, **kwargs)

    def _invoke_with_tools(self, messages, tools, tool_choice, **kwargs):
        # 带工具的调用绕过 llm.invoke 直连 OpenAI 客户端，须在此补瞬态重试
        def attempt():
            consume_budget("llm_calls")
            options = dict(kwargs)
            timeout = remaining_timeout(float(options.pop("timeout", self.llm.timeout)))
            options.setdefault("temperature", self.llm.temperature)
            if self.llm.max_tokens is not None:
                options.setdefault("max_tokens", self.llm.max_tokens)
            response = _request_client(self.llm, timeout).chat.completions.create(
                model=self.llm.model, messages=messages, tools=tools,
                tool_choice=tool_choice, timeout=timeout, **options)
            check_budget()
            choices = getattr(response, "choices", None) or []
            if not choices:
                raise IncompleteResponseError("empty_choices")
            choice = choices[0]
            reason = getattr(choice, "finish_reason", None)
            if reason == "content_filter":
                raise LLMCallError("content_filter")
            if reason not in ("stop", "tool_calls"):
                raise IncompleteResponseError(f"finish_{reason or 'missing'}")
            message = choice.message
            calls = getattr(message, "tool_calls", None) or []
            if not calls and not self._extract_message_content(message.content).strip():
                raise IncompleteResponseError("empty_response")
            for call in calls:
                try:
                    parsed = json.loads(call.function.arguments)
                    if not isinstance(parsed, dict):
                        raise ValueError("object required")
                except (ValueError, TypeError):
                    raise IncompleteResponseError("invalid_tool_arguments")
            return response
        return _retry_transient(attempt, "LLM 工具调用")


# ============================================================
# 专职 Agent
# ============================================================

class UnitBuildAgent(PipelineAgent):
    """翻译单元划分 Agent（原 UnitBuilder 的 LLM 部分）"""

    SYSTEM_PROMPT = """你是一个Android项目架构分析专家。你的任务是将给定的文件列表分组为"翻译单元(Unit)"。

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

## 硬绑定约束（必须遵守）
输入中会给出"硬绑定组"——通过静态分析确定的强耦合文件组
（如 Activity 与它 setContentView 的布局、Adapter 与它 inflate 的 item 布局、布局与它 include 的子布局）。
每个硬绑定组内的文件必须放在同一个单元中，不允许拆散。可以把多个硬绑定组合并进同一个单元，但不能拆分。

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
3. 确保所有文件都被分配到某个单元中
4. 硬绑定组内的文件必须在同一单元"""

    def __init__(self, llm: HelloAgentsLLM, tools):
        super().__init__(
            name="Unit划分Agent", llm=llm, tools=tools,
            system_prompt=self.SYSTEM_PROMPT, max_tool_iterations=6,
        )


class DependencyReviewAgent(PipelineAgent):
    """Unit 依赖审核补充 Agent（原 UnitDependencyAnalyzer 的 LLM 第二遍）"""

    SYSTEM_PROMPT = """你是一个Java项目依赖分析专家。你的任务是审核并补充翻译单元之间的依赖关系。

## 背景
已经通过静态分析（AST 类型引用、import、Intent 跳转、布局引用）得到了初步依赖图。
静态分析仍可能遗漏的情况：
1. 通过反射、字符串类名动态加载
2. 隐式协议依赖（共享 SharedPreferences key、广播 action、数据库表结构）

## 可用工具
- get_file_summary: 查看文件详细摘要
- read_file_region: 读取文件指定行

## 任务
基于给出的静态依赖结果，补充确实存在但被遗漏的依赖。不要删除静态分析得到的依赖。

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
- 边方向：键是依赖方，值是它依赖的单元列表。"单元A": ["单元B"] 表示 A 依赖 B（B 必须先于 A 翻译）
- 单元名必须与输入中给定的完全一致
- 只输出确实存在的依赖，宁缺毋滥"""

    def __init__(self, llm: HelloAgentsLLM, tools):
        super().__init__(
            name="依赖审核Agent", llm=llm, tools=tools,
            system_prompt=self.SYSTEM_PROMPT, max_tool_iterations=6,
        )


class TranslationPlanAgent(PipelineAgent):
    """翻译方案规划 Agent（原 UnitTranslator 的 Planner）"""

    SYSTEM_PROMPT = """你是一个 Android → HarmonyOS 代码翻译规划专家。

## 任务
分析翻译单元中的源文件，决定:
1. 输出几个 .ets 文件及其顺序
2. 每个文件分几步翻译
3. 每一步需要读取哪些源文件的哪些行

## 可用工具
- get_all_summaries(): 列出项目文件
- get_file_summary(文件名): 查看文件详细摘要（方法签名、行号、字段、资源引用等）

## 决策规则

### 输出依赖
- depends_on 使用准确的 .ets 文件名：可以引用依赖上下文中已生成的文件，也可以引用本次 outputs 中的其他文件。
- 不得依赖自己、未声明的文件或臆测的文件名。order 按依赖先于使用者排列。
- 本次源文件可能包含一组循环依赖的单元，必须整体规划并覆盖所有源码。页面间跳转使用路由；共享数据/接口提取为基础模块，避免输出文件之间循环依赖。

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
{
  "outputs": [
    {
      "file": "DbHelper.ets",
      "order": 0,
      "type": "simple",
      "sources": ["DbHelper.java"],
      "depends_on": [],
      "description": "数据库工具类"
    },
    {
      "file": "MainPage.ets",
      "order": 0,
      "type": "planned",
      "sources": ["MainActivity.java", "activity_main.xml"],
      "plan": [
        {
          "step": "翻译 import 和 @State 变量",
          "read_regions": [
            {"file": "MainActivity.java", "lines": "L1-L35"},
            {"file": "activity_main.xml", "lines": "L1-L30"}
          ]
        },
        {
          "step": "翻译 build() 方法",
          "read_regions": [
            {"file": "activity_main.xml", "lines": "L1-L40"}
          ]
        }
      ],
      "depends_on": [],
      "description": "欢迎页面"
    }
  ]
}

只输出 JSON。"""

    def __init__(self, llm: HelloAgentsLLM, tools):
        super().__init__(
            name="翻译规划Agent", llm=llm, tools=tools,
            system_prompt=self.SYSTEM_PROMPT, max_tool_iterations=6,
        )


class UnitTranslateAgent(PipelineAgent):
    """带工具的翻译回退 Agent（原 UnitTranslator 的 Executor 工具回退路径）

    正常翻译走无工具的单轮 invoke（源码已组装进 prompt）；
    当输出解析失败时，用本 Agent 允许 LLM 借助 read_file_region /
    lookup_resource 等工具补充信息后重试。
    """

    SYSTEM_PROMPT = "你是一个 Java/XML → ArkTS 代码翻译专家。可在需要时调用工具补充源码或资源映射信息。"

    def __init__(self, llm: HelloAgentsLLM, tools):
        super().__init__(
            name="翻译执行Agent", llm=llm, tools=tools,
            system_prompt=self.SYSTEM_PROMPT, max_tool_iterations=3,
        )

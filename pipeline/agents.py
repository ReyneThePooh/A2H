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
from dataclasses import dataclass
from typing import Any, Optional

from hello_agents.core.llm import HelloAgentsLLM
from hello_agents.agents.function_call_agent import FunctionCallAgent


# ============================================================
# LLM 工厂
# ============================================================

def create_pipeline_llm(temperature: float = 0.3) -> HelloAgentsLLM:
    """构造流水线使用的 HelloAgentsLLM。

    HelloAgentsLLM 与原 LLMClient 读取相同的环境变量
    （LLM_MODEL_ID / LLM_API_KEY / LLM_BASE_URL / LLM_TIMEOUT），
    仅超时默认值不同，这里显式保持流水线原默认 180 秒。
    """
    return HelloAgentsLLM(
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

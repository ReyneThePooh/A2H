"""
HelloAgents - 灵活、可扩展的多智能体框架

基于OpenAI原生API构建，提供简洁高效的智能体开发体验。
"""

# 配置第三方库的日志级别，减少噪音
import logging
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("qdrant_client").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("neo4j").setLevel(logging.WARNING)
logging.getLogger("neo4j.notifications").setLevel(logging.WARNING)

from .version import __version__, __author__, __email__, __description__

# 核心组件
from .core.llm import HelloAgentsLLM
from .core.config import Config
from .core.message import Message
from .core.exceptions import HelloAgentsException

# 可选 Agent 和工具会间接依赖评测、搜索等扩展包。将它们惰性加载，
# 使仅使用核心 LLM 的翻译流程不要求安装所有可选依赖。
_LAZY_IMPORTS = {
    "SimpleAgent": (".agents.simple_agent", "SimpleAgent"),
    "ReActAgent": (".agents.react_agent", "ReActAgent"),
    "ReflectionAgent": (".agents.reflection_agent", "ReflectionAgent"),
    "PlanAndSolveAgent": (".agents.plan_solve_agent", "PlanAndSolveAgent"),
    "ToolAwareSimpleAgent": (".agents.tool_aware_agent", "ToolAwareSimpleAgent"),
    "ToolRegistry": (".tools.registry", "ToolRegistry"),
    "global_registry": (".tools.registry", "global_registry"),
    "SearchTool": (".tools.builtin.search_tool", "SearchTool"),
    "search": (".tools.builtin.search_tool", "search"),
    "CalculatorTool": (".tools.builtin.calculator", "CalculatorTool"),
    "calculate": (".tools.builtin.calculator", "calculate"),
    "ToolChain": (".tools.chain", "ToolChain"),
    "ToolChainManager": (".tools.chain", "ToolChainManager"),
    "AsyncToolExecutor": (".tools.async_executor", "AsyncToolExecutor"),
}


def __getattr__(name):
    """在首次访问可选组件时再导入其实现。"""
    if name not in _LAZY_IMPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    from importlib import import_module

    module_name, attribute_name = _LAZY_IMPORTS[name]
    value = getattr(import_module(module_name, __name__), attribute_name)
    globals()[name] = value
    return value

__all__ = [
    # 版本信息
    "__version__",
    "__author__",
    "__email__",
    "__description__",

    # 核心组件
    "HelloAgentsLLM",
    "Config",
    "Message",
    "HelloAgentsException",

    # Agent范式
    "SimpleAgent",
    "ReActAgent",
    "ReflectionAgent",
    "PlanAndSolveAgent",
    "ToolAwareSimpleAgent",

    # 工具系统
    "ToolRegistry",
    "global_registry",
    "SearchTool",
    "search",
    "CalculatorTool",
    "calculate",
    "ToolChain",
    "ToolChainManager",
    "AsyncToolExecutor",
]


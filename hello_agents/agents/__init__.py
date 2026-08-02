"""Agent实现模块 - HelloAgents原生Agent范式"""

_LAZY_IMPORTS = {
    "SimpleAgent": (".simple_agent", "SimpleAgent"),
    "FunctionCallAgent": (".function_call_agent", "FunctionCallAgent"),
    "ReActAgent": (".react_agent", "ReActAgent"),
    "ReflectionAgent": (".reflection_agent", "ReflectionAgent"),
    "PlanAndSolveAgent": (".plan_solve_agent", "PlanAndSolveAgent"),
    "ToolAwareSimpleAgent": (".tool_aware_agent", "ToolAwareSimpleAgent"),
}


def __getattr__(name):
    """避免导入单个 Agent 时加载所有可选工具与评测依赖。"""
    if name not in _LAZY_IMPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    from importlib import import_module

    module_name, attribute_name = _LAZY_IMPORTS[name]
    value = getattr(import_module(module_name, __name__), attribute_name)
    globals()[name] = value
    return value

__all__ = [
    "SimpleAgent",
    "FunctionCallAgent",
    "ReActAgent",
    "ReflectionAgent",
    "PlanAndSolveAgent",
    "ToolAwareSimpleAgent",
]

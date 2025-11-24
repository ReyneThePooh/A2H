import re
import ast
from typing import Optional, List, Tuple
from ..core.agent import Agent
from ..core.llm import HelloAgentsLLM
from ..core.config import Config
from ..core.message import Message
from ..tools.registry import ToolRegistry

# 知识收集提示词模板
DEFAULT_THINK_PROMPT = """
你是一个顶级的Java到ArkTS代码转换知识分析专家。你的任务是分析Java代码，识别转换为ArkTS时需要查询的关键知识点。
请确保生成的问题都是语言特性层面的、通用的、可独立检索的知识点。
你的输出必须是一个Python列表，其中每个元素都是一个需要查询的问题字符串。

## 翻译任务
{translate_task}

## Java代码
```java
{java_code}

分析要求:
    -聚焦当前步骤：只分析完成当前翻译步骤所需的知识，忽略其他部分
    -识别关键特性：找出当前步骤涉及的Java特性及其ArkTS对应方案
    -保持通用性：问题应该是"XX特性如何转换"，而不是具体实现细节
    -避免过度分析：不要分析当前步骤不涉及的代码部分
    -不要提出过于简单的问题，各问题之间应该相互独立

如果翻译任务为：实现Java Serializable接口

Java代码包含：
    -实现了Serializable接口
    -使用了List<String>泛型
    -有私有字段和public方法
    -使用了@Override注解

则输出：
```python
["Java Serializable接口如何转换为ArkTS"]
```

不应该输出与当前翻译任务不强相关的问题如：
 "Java泛型List如何转换为ArkTS数组" 
 "Java注解如何转换为ArkTS"
 "Java类的访问修饰符如何转换为ArkTS"

请严格按照以下格式输出你的问题列表：
```python
["问题1", "问题2", "问题3", ...]
```
"""

class Think:
    def __init__(self, llm_client: HelloAgentsLLM, prompt_template: Optional[str] = None):
        self.llm_client = llm_client
        self.prompt_template = prompt_template if prompt_template else DEFAULT_THINK_PROMPT

    def think(self, translate_task: str, **kwargs)->List[str]:
        """
        生成检索问题

        Args:
            translate_task: 翻译任务
            **kwargs: LLM调用参数

        Returns:
            步骤列表
        """
        prompt = self.prompt_template.format(translate_task=translate_task, java_code=kwargs.get('java_code', ""))
        messages = [{"role": "user", "content": prompt}]

        print("--- 正在生成问题 ---")
        response_text = self.llm_client.invoke(messages) or ""
        print(f"✅ 问题已生成:\n{response_text}")

        try:
            # 提取Python代码块中的列表
            plan_str = response_text.split("```python")[1].split("```")[0].strip()
            plan = ast.literal_eval(plan_str)
            return plan if isinstance(plan, list) else []
        except (ValueError, SyntaxError, IndexError) as e:
            print(f"❌ 解析出错: {e}")
            print(f"原始响应: {response_text}")
            return []
        except Exception as e:
            print(f"❌ 解析发生未知错误: {e}")
            return []


class KnowledgeCollectionAgent(Agent):
    """
    简化的知识收集Agent

    专注于：分析代码 → RAG检索 → 收集知识 → 返回结果
    """

    def __init__(
            self,
            name: str,
            llm: HelloAgentsLLM,
            tool_registry: Optional[ToolRegistry] = None,
            system_prompt: Optional[str] = None,
            config: Optional[Config] = None,
            max_queries: int = 3,
            custom_prompt: Optional[str] = None
    ):
        """
        初始化知识收集Agent

        Args:
            name: Agent名称
            llm: LLM实例
            tool_registry: 工具注册表（必须包含RAG工具）
            system_prompt: 系统提示词
            config: 配置对象
            max_queries: 最大查询次数（默认3次）
            custom_prompt: 自定义提示词模板
        """
        super().__init__(name, llm, system_prompt, config)

        if tool_registry is None:
            self.tool_registry = ToolRegistry()
        else:
            self.tool_registry = tool_registry

        self.max_queries = max_queries
        self.current_history: List[str] = []
        self.prompt_template = custom_prompt if custom_prompt else DEFAULT_PROMPT

    def run(self, java_code: str, translate_question: str = "") -> str:
        """
        收集Java到ArkTS转换所需的知识

        Args:
            java_code: 需要转换的Java代码
            translate_question: 翻译任务描述（可选）

        Returns:
            str: 收集到的知识内容（如果不需要查询则返回空字符串）
        """
        self.current_history = []
        query_count = 0
        collected_knowledge = []

        print(f"\n🔍 {self.name} 开始分析Java代码，收集转换知识...")
        print(f"📋 翻译任务: {translate_question if translate_question else '通用转换'}")

        while query_count < self.max_queries:
            query_count += 1
            print(f"\n--- 查询轮次 {query_count}/{self.max_queries} ---")

            # 构建提示词
            tools_desc = self.tool_registry.get_tools_description()
            history_str = "\n".join(self.current_history)
            prompt = self.prompt_template.format(
                translate_question=translate_question,
                tools=tools_desc,
                question=java_code,
                history=history_str
            )

            # 调用LLM
            messages = [{"role": "user", "content": prompt}]
            response_text = self.llm.invoke(messages)

            if not response_text:
                print("❌ LLM未返回有效响应，终止收集")
                break

            print(f"\n🤖 Agent响应:\n{response_text}\n")

            # 解析输出
            thought, action = self._parse_output(response_text)

            if thought:
                print(f"💭 思考: {thought}")

            if not action:
                print("⚠️ 未解析到有效Action，终止收集")
                break

            # 检查是否完成
            if action.startswith("Finish"):
                final_knowledge = self._parse_action_input(action)
                print(f"\n✅ 知识收集完成")

                # 保存到历史记录
                self.add_message(Message(java_code, "user"))
                self.add_message(Message(final_knowledge, "assistant"))

                # 判断是否真的收集到了知识
                if not final_knowledge or final_knowledge.strip() == "":
                    print("📝 无需额外知识")
                    return ""

                print(f"📚 收集到的知识:\n{final_knowledge}")
                return final_knowledge

            # 执行RAG查询
            tool_name, tool_input = self._parse_action(action)
            if not tool_name or tool_input is None:
                self.current_history.append("Observation: 无效的Action格式，请使用正确格式")
                continue

            print(f"🔎 执行查询: {tool_name}[{tool_input}]")

            # 调用RAG工具
            try:
                observation = self.tool_registry.execute_tool(tool_name, tool_input)
                print(f"📖 检索结果: {observation[:200]}..." if len(observation) > 200 else f"📖 检索结果: {observation}")

                # 收集知识
                collected_knowledge.append({
                    "query": tool_input,
                    "result": observation
                })

                # 更新历史
                self.current_history.append(f"Action: {action}")
                self.current_history.append(f"Observation: {observation}")

            except Exception as e:
                error_msg = f"工具调用失败: {str(e)}"
                print(f"❌ {error_msg}")
                self.current_history.append(f"Observation: {error_msg}")

        # 达到最大查询次数
        print(f"\n⏰ 已达到最大查询次数 ({self.max_queries})，结束收集")

        if collected_knowledge:
            # 合并所有收集到的知识
            final_knowledge = self._merge_knowledge(collected_knowledge)

            # 保存到历史记录
            self.add_message(Message(java_code, "user"))
            self.add_message(Message(final_knowledge, "assistant"))

            print(f"📚 共收集 {len(collected_knowledge)} 条知识")
            return final_knowledge

        print("📝 未收集到任何知识")
        return ""

    def _merge_knowledge(self, knowledge_list: List[dict]) -> str:
        """合并收集到的知识"""
        if not knowledge_list:
            return ""

        merged = "已收集的转换规则：\n\n"
        for i, item in enumerate(knowledge_list, 1):
            merged += f"## 知识点 {i}: {item['query']}\n"
            merged += f"{item['result']}\n\n"

        return merged.strip()

    def _parse_output(self, text: str) -> Tuple[Optional[str], Optional[str]]:
        """解析LLM输出，提取思考和行动"""
        # 提取Thought（可能跨多行）
        thought_match = re.search(r"Thought:\s*(.*?)(?=Action:|Finish\[|$)", text, re.DOTALL | re.IGNORECASE)
        thought = thought_match.group(1).strip() if thought_match else None

        # 提取Action或Finish
        action_match = re.search(r"(Action:\s*\S+\[.*?\]|Finish\[.*?\])", text, re.DOTALL | re.IGNORECASE)
        action = action_match.group(1).strip() if action_match else None

        # 去掉Action:前缀
        if action and action.startswith("Action:"):
            action = action[7:].strip()

        return thought, action

    def _parse_action(self, action_text: str) -> Tuple[Optional[str], Optional[str]]:
        """解析行动文本，提取工具名称和输入"""
        match = re.match(r"(\w+)\[(.*)\]", action_text, re.DOTALL)
        if match:
            tool_name = match.group(1).strip()
            tool_input = match.group(2).strip()
            return tool_name, tool_input
        return None, None

    def _parse_action_input(self, action_text: str) -> str:
        """解析Finish[]中的内容"""
        match = re.match(r"Finish\[(.*)\]", action_text, re.DOTALL)
        return match.group(1).strip() if match else ""
from typing import Optional
from ..core.agent import Agent
from ..core.llm import HelloAgentsLLM
from ..core.config import Config


DEFAULT_PROMPT = """
你是一位专业的Java代码依赖分析专家。你的任务是分析给定的安卓项目目录结构以及Java代码，识别其所有依赖关系，并以绝对路径的形式输出。

# 分析规则:
1. 识别所有import语句中的依赖
2. 识别类继承关系（extends关键字）
3. 识别接口实现关系（implements关键字）
4. 识别类中使用的自定义类型（字段类型、方法参数、方法返回值等）
5. 排除Java标准库以及第三方库的依赖（如java.util.*, java.lang.*等）
6. 将包名转换为绝对路径格式（用反斜杠\分隔，并添加.java后缀）

# 输入的安卓目录结构：
{android_project_tree}

# 输入的Java代码:
{java_code}

# 输出格式要求:
请仅输出依赖的Java文件绝对路径列表，每行一个路径，不要包含任何解释或额外信息。
路径格式示例: D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\bean\\BaseBean.java

# 依赖列表:
"""


class DependencyAnalyzeAgent(Agent):
    def __init__(
            self,
            name: str,
            llm: HelloAgentsLLM,
            system_prompt: Optional[str] = None,
            config: Optional[Config] = None,
    ):
        """
        初始化DependencyAnalyzeAgent

        Args:
            name: Agent名称
            llm: LLM实例
            system_prompt: 系统提示词（如果不提供，使用DEFAULT_PROMPT）
            config: 配置对象
        """
        if system_prompt is None:
            system_prompt = DEFAULT_PROMPT

        super().__init__(name, llm, system_prompt, config)

    def run(self, input_text: str, **kwargs) -> str:
        """
        分析Java代码的依赖关系

        Args:
            input_text: 格式为 "目录树|||文件内容"
            **kwargs: 其他参数传递给LLM

        Returns:
            依赖文件的绝对路径列表（每行一个路径）
        """
        # 分割目录树和代码内容
        parts = input_text.split('|||', 1)
        if len(parts) != 2:
            raise ValueError("input_text格式错误，应为: 文件路径|||文件内容")

        android_project_tree = parts[0].strip()
        java_code = parts[1].strip()

        # 将目录树和代码填充到提示词模板中
        prompt = self.system_prompt.format(
            android_project_tree=android_project_tree,
            java_code=java_code
        )

        # print(prompt)

        # 构建消息
        messages = [{"role": "user", "content": prompt}]

        # 调用LLM获取响应
        response = self.llm.invoke(messages, **kwargs)

        return response

    def analyze_dependencies(self, input: str, **kwargs) -> list[str]:
        """
        分析Java代码依赖并返回路径列表

        Args:
            input: 要分析的Java文件路径以及Java代码，格式为 "目录树|||文件内容"
            **kwargs: 其他参数传递给LLM

        Returns:
            依赖文件路径列表
        """
        response = self.run(input, **kwargs)

        # 解析响应，提取路径列表
        paths = []
        for line in response.strip().split('\n'):
            line = line.strip()
            # 过滤掉空行和非路径行
            if line and line.endswith('.java'):
                paths.append(line)

        return paths


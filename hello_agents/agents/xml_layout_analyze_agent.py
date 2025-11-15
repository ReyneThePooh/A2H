from typing import Optional

from ..core.agent import Agent
from ..core.llm import HelloAgentsLLM
from ..core.config import Config


DEFAULT_PROMPT = """
你是一位专业的Android布局关联分析专家。你的任务是分析给定的安卓目录结构以及Java代码，识别其所有引用的XML布局文件，并以绝对路径的形式输出。

# 分析规则:
1. 识别 setContentView(R.layout.xxx) 的布局名
2. 识别 LayoutInflater.inflate(R.layout.xxx, ...) 与 View.inflate(..., R.layout.xxx, ...)
3. 识别 Adapter、Fragment、Dialog 等中调用 inflate(R.layout.xxx, ...)
4. 识别 DataBinding 与 ViewBinding 使用的绑定类，例如 ActivityMainBinding.inflate(...) 对应 activity_main.xml，ItemUserBinding.bind(...) 对应 item_user.xml
5. 识别自定义视图或任意方法中通过资源ID引用布局的场景
6. 仅输出与项目相关的布局文件；排除第三方库中的引用
7. 每行输出一个路径，不添加任何解释

# 输入的安卓目录结构：
{android_project_tree}

# 输入的Java代码:
{java_code}

# 输出格式要求:
仅输出布局文件的绝对路径列表，每行一个路径，不要包含任何解释。
路径格式示例: D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\res\\layout\\activity_main.xml
# 布局文件列表:
"""


class XMLLayoutAnalyzeAgent(Agent):
    def __init__(
            self,
            name: str,
            llm: HelloAgentsLLM,
            system_prompt: Optional[str] = None,
            config: Optional[Config] = None,
    ):
        if system_prompt is None:
            system_prompt = DEFAULT_PROMPT

        super().__init__(name, llm, system_prompt, config)

    def run(self, input_text: str, **kwargs) -> str:
        parts = input_text.split('|||', 1)
        if len(parts) != 2:
            raise ValueError("input_text格式错误，应为: 文件路径|||文件内容")

        android_project_tree = parts[0].strip()
        java_code = parts[1].strip()

        prompt = self.system_prompt.format(
            android_project_tree=android_project_tree,
            java_code=java_code
        )

        messages = [{"role": "user", "content": prompt}]
        response = self.llm.invoke(messages, **kwargs)
        return response

    def analyze_layouts(self, input: str, **kwargs) -> list[str]:
        response = self.run(input, **kwargs)

        paths = []
        for line in response.strip().split('\n'):
            line = line.strip()
            if line and line.endswith('.xml'):
                paths.append(line)

        return paths
from typing import Optional

from ..core.agent import Agent
from ..core.llm import HelloAgentsLLM
from ..core.config import Config

DEFAULT_PROMPT =  """
你是一位专业的Android项目架构分析专家。你的任务是分析给定的Java代码和XML布局文件，将它们划分为功能单元，并按照业务逻辑顺序排列。

# 功能单元定义:
一个功能单元是指在业务上相关的一组文件，通常包括：
- Activity/Fragment 及其对应的布局文件
- 该页面使用的 Adapter 及其 item 布局
- 该页面使用的自定义 View 及其布局
- 该页面使用的 Dialog/PopupWindow 及其布局
- 相关的辅助类（如果紧密耦合）

# 分析要求:
1. 识别所有功能模块（如：登录模块、主页模块、设置模块等）
2. 将每个模块的相关Java文件和XML文件归类到同一个功能单元
3. **按照用户使用流程和业务逻辑排序**：
   - 启动页、欢迎页等入口页面在前
   - 登录/注册等认证页面其次
   - 主页面/导航页面居中
   - 核心功能页面（列表、详情等）随后
   - 设置、关于等辅助页面在后
4. 对于同类型的功能（如多个列表页），按照业务重要性排序
5. **只处理布局文件，不要包含资源文件（如strings.xml、colors.xml、styles.xml、dimens.xml等）**
6. 为每个功能单元提供简短的描述，说明该单元的主要功能

# 输入的Java文件:
{java_dict}

# 输入的XML文件:
{xml_dict}

# 输出格式要求:
严格按照以下JSON格式输出，不要添加任何其他文本：
[
  {{
    "description": "功能单元的简短描述，如：应用启动页",
    "java_files": ["java文件绝对路径1", "java文件绝对路径2"],
    "xml_files": ["xml布局文件绝对路径1", "xml布局文件绝对路径2"]
  }},
  {{
    "description": "功能单元的简短描述，如：用户登录页面",
    "java_files": ["java文件绝对路径1"],
    "xml_files": ["xml布局文件绝对路径1"]
  }},
  {{
    "description": "功能单元的简短描述，如：主页面及导航",
    "java_files": ["java文件绝对路径1"],
    "xml_files": ["xml布局文件绝对路径1"]
  }}
]

注意：
- 列表已按业务流程顺序排列，遵循用户实际使用路径
- 每个文件路径必须与输入中的路径完全一致
- 必须是有效的JSON格式
- 确保所有输入的文件都被分配到某个功能单元中
- **xml_files中只能包含布局文件，不能包含资源文件（如strings.xml、colors.xml等）**
- description应简洁明了，便于快速定位功能单元

# 功能单元列表:
"""


class FunctionalUnitAnalyzeAgent(Agent):
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

    def run(self, java_dict: str, xml_dict: str, **kwargs) -> str:
        """
        分析Java和XML文件，识别功能单元

        Args:
            java_dict: Java文件字典的字符串表示
            xml_dict: XML文件字典的字符串表示
        """
        prompt = self.system_prompt.format(
            java_dict=java_dict,
            xml_dict=xml_dict
        )

        messages = [{"role": "user", "content": prompt}]
        response = self.llm.invoke(messages, **kwargs)
        return response

    def analyze_units(self, java_dict_str: str, xml_dict_str: str, **kwargs) -> list[dict]:
        """
        分析功能单元

        Args:
            java_dict_str: Java文件字典的字符串表示
            xml_dict_str: XML文件字典的字符串表示

        Returns:
            功能单元列表，每个单元包含description, java_files, xml_files
        """
        response = self.run(java_dict=java_dict_str, xml_dict=xml_dict_str, **kwargs)

        # 解析JSON响应
        import json
        try:
            # 尝试提取JSON部分（有时LLM会在前后添加额外文本）
            response = response.strip()
            start_idx = response.find('[')
            end_idx = response.rfind(']') + 1
            if start_idx != -1 and end_idx > start_idx:
                json_str = response[start_idx:end_idx]
                units = json.loads(json_str)
                return units
            else:
                raise ValueError("响应中未找到有效的JSON数组")
        except json.JSONDecodeError as e:
            print(f"JSON解析失败: {e}")
            print(f"原始响应: {response}")
            return []
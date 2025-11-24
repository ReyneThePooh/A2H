from typing import Optional
from ..core.agent import Agent
from ..core.llm import HelloAgentsLLM
from ..core.config import Config


DEFAULT_PROMPT = RESOURCE_DEPENDENCY_PROMPT = RESOURCE_DEPENDENCY_PROMPT = """
你是一位专业的Android项目资源依赖分析专家。你的任务是分析给定的Android项目目录结构以及Java/XML代码，识别其所依赖的资源文件，并以绝对路径的形式输出。

# 分析规则:

## Java代码资源依赖识别参考:
1. R.layout.* - 布局文件（res/layout/目录）
2. R.drawable.* - 图片资源（res/drawable/目录及其变体）
3. R.string.* - 字符串资源（res/values/strings.xml）
4. R.color.* - 颜色资源（res/values/colors.xml）
5. R.dimen.* - 尺寸资源（res/values/dimens.xml）
6. R.style.* - 样式资源（res/values/styles.xml）
7. R.menu.* - 菜单资源（res/menu/目录）
8. R.anim.* - 动画资源（res/anim/目录）
9. R.raw.* - 原始资源（res/raw/目录）
10. R.xml.* - XML资源（res/xml/目录）
11. R.mipmap.* - 应用图标（res/mipmap/目录及其变体）
12. Assets文件引用（如AssetManager.open("path/file")）

## XML代码资源依赖识别参考:
1. @layout/ - 引用的布局文件
2. @drawable/ - 引用的图片资源
3. @string/ - 引用的字符串
4. @color/ - 引用的颜色
5. @dimen/ - 引用的尺寸
6. @style/ - 引用的样式
7. @menu/ - 引用的菜单
8. @anim/ - 引用的动画
9. @mipmap/ - 引用的图标
10. android:background、android:src等属性值
11. <include layout="@layout/..." /> 引用

## 处理规则:
- 只输出实际存在于目录树中的资源文件
- 排除Android系统资源（android.R.*、@android:*）
- 将资源ID转换为对应的文件绝对路径
- 对于有多个密度/配置的资源（如drawable-hdpi、drawable-xhdpi），列出所有变体

# 输入的Android项目目录结构：
{android_project_tree}

# 输入的Java代码（如果为空则不处理）:
{java_code}

# 输入的XML代码（如果为空则不处理）:
{xml_code}

# 输出格式要求:
严格按照以下格式输出：
1. 如果找到资源依赖：每行输出一个绝对路径，不要有任何其他文字
2. 如果没有找到任何资源依赖：输出 NONE
3. 不要输出解释、说明、分析过程等任何额外内容

正确示例1（有依赖）:
D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\res\\layout\\activity_main.xml
D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\res\\drawable-hdpi\\ic_launcher.png

正确示例2（无依赖）:
NONE

错误示例（禁止这样输出）:
根据提供的Android项目目录结构和代码分析，当前输入的Java代码和XML代码中均未发现任何资源依赖引用。

# 资源依赖列表:
"""


class ResourceAnalyzeAgent(Agent):
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

    def run(self, project_structure: str, **kwargs) -> str:
        """
        分析Java代码的依赖关系

        Args:
            project_structure: 目录结构
            **kwargs: 其他参数传递给LLM

        Returns:
            依赖文件的绝对路径列表（每行一个路径）
        """
        # 分割目录树和代码内容
        java_code = kwargs.get('java_code', "")
        xml_code = kwargs.get('xml_code', "")

        # 将目录树和代码填充到提示词模板中
        prompt = self.system_prompt.format(
            android_project_tree=project_structure,
            java_code=java_code,
            xml_code=xml_code
        )

        # print(prompt)

        # 构建消息
        messages = [{"role": "user", "content": prompt}]

        # 调用LLM获取响应
        response = self.llm.invoke(messages)

        return response

    def analyze_resources(self, project_structure: str, **kwargs) -> list[str]:
        """
        分析Java代码或XML代码资源依赖并返回路径列表

        Args:
            project_structure: 目录结构
            **kwargs: 其他参数传递给LLM

        Returns:
            依赖文件路径列表
        """
        java_code = kwargs.get('java_code', "")
        xml_code = kwargs.get('xml_code', "")
        response = self.run(project_structure, java_code=java_code, xml_code=xml_code)

        # 解析响应，提取路径列表
        paths = []
        for line in response.strip().split('\n'):
            line = line.strip()
            # 过滤掉空行和非路径行
            if line.upper() == 'NONE':
                continue
            paths.append(line)

        return paths


from typing import Optional

from ..core.agent import Agent
from ..core.llm import HelloAgentsLLM
from ..core.config import Config


DEFAULT_PROMPT = """
你是一位专业的Android代码分析专家。你的任务是分析给定的Java代码，识别哪些Java文件是交互类。

# 判断标准:

## 交互类（与UI交互强相关）
定义：直接处理用户界面操作、响应交互事件和管理页面导航的类，负责将用户输入转换为应用行为并呈现可视化反馈。

特征：
1. 直接绑定到具体的用户界面组件
2. 处理用户输入事件（点击、滑动、长按等）
3. 管理页面生命周期和状态（onCreate、onResume等）
4. 控制页面间的跳转流程（startActivity、FragmentTransaction等）
5. 协调数据展示和用户操作
6. 使用 setContentView、findViewById、ViewBinding等UI绑定方法
7. 实现事件监听器（OnClickListener、TextWatcher等）

示例：
- Activity类（MainActivity, DiaryListActivity等）
- Fragment类
- Adapter类（DiaryRecyclerAdapter, BaseAdapter等）
- ViewHolder类
- 自定义View类（直接处理用户交互的）
- Dialog、PopupWindow等UI组件管理类

## 非交互类（支持类）
定义：提供应用程序基础功能、数据处理和工具服务的类，不直接处理用户界面交互。

特征：
- 工具类（TimeUtils, ToastUtils, AppUtils, SizeUtils, StringUtils）
- 数据模型类（Diary, User, Entity等POJO）
- 应用配置类（Mapp, Config, Constants）
- 基础框架类（BaseActivity, BaseFragment）
- 网络请求类（ApiService, NetworkHelper）
- 数据库操作类（DatabaseHelper, DAO）
- Service、BroadcastReceiver等后台组件
- 接口定义（Callback, Listener接口）

# 输入的Java文件及代码:
{java_dict}

# 输出格式要求:
仅输出交互类的Java文件的绝对路径列表，每行一个路径，不要包含任何解释。
如果所有文件都不是交互类则返回空。
路径格式示例: D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\example\\MainActivity.java

# 交互类文件列表：
"""

class InteractionAnalyzeAgent(Agent):
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
        prompt = self.system_prompt.format(
            java_dict=input_text
        )

        messages = [{"role": "user", "content": prompt}]
        response = self.llm.invoke(messages, **kwargs)
        return response

    def analyze_layouts(self, java_dict_str: str, **kwargs) -> list[str]:
        response = self.run(java_dict_str, **kwargs)

        paths = []
        for line in response.strip().split('\n'):
            line = line.strip()
            if line and line.endswith('.java'):
                paths.append(line)

        return paths
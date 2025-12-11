"""Plan and Solve Agent实现 - 分解规划与逐步执行的智能体"""
import ast
import json
from typing import Optional, List, Dict
from ..core.agent import Agent
from ..core.llm import HelloAgentsLLM
from ..core.config import Config
from ..core.message import Message


# 规划器提示词
DEFAULT_PLANNER_PROMPT = """
你是一个顶级的代码翻译规划专家。你的任务是将Java代码和XML布局代码翻译成ArkTS代码的过程分解成一个由多个简单步骤组成的行动计划，并识别需要参考的知识文件。

完整Java代码:
{java_code}

XML代码(可能为空):
{xml_code}

翻译上下文信息:
- 源语言: Java (Android开发语言) + XML (Android布局)
- 目标语言: ArkTS (HarmonyOS开发语言)
- 输出目标: 一个完整的.ets文件,包含UI声明和业务逻辑

翻译策略:

情况1: 支持类(纯Java,无XML)
  定义: 工具类、数据模型、基础框架、配置类等不直接处理UI交互的类
  特征: 无setContentView、无findViewById、无事件监听器、无XML布局
  
  - 这类代码通常结构简单,不要过度拆分
  - 推荐步骤数: 3-5步
  - 典型流程:
    步骤1: 翻译导入语句和类声明
    步骤2: 翻译成员变量、常量和接口定义
    步骤3: 翻译所有工具方法和业务逻辑(可按功能分组,但不要每个方法一个步骤)

情况2: 交互类(包含Java和XML)
  定义: Activity、Fragment、Adapter、自定义View等直接处理用户界面操作和交互事件的类
  特征: 使用setContentView、findViewById、事件监听器、页面生命周期管理
  
  - 这类代码涉及UI和逻辑的协同翻译
  - 推荐步骤数: 5-8步
  - 核心原则: Java和XML需要协同分析,不能孤立翻译
  
  典型流程:
    步骤1: 翻译导入语句和类声明为@Component或@Entry
    步骤2: 分析XML布局和Java中的findViewById引用,翻译状态变量(@State/@Prop)
    步骤3: 翻译辅助方法和数据处理逻辑
    步骤4: 翻译build()方法,将XML布局转换为声明式UI(Column/Row/List等)
    步骤5: 翻译事件处理方法(onClick等)和生命周期方法(aboutToAppear等)

知识文件使用原则:
  1. **匹配代码特征**: 根据Java/XML代码的特征选择相关知识点
  2. **按需引用**: 只在步骤确实需要相关知识时才引用
  3. **层级引用**: 基础语法优先,组件和布局其次,高级特性最后
  4. **避免冗余**: 不要引用明显不相关的知识文件
  5. **严格限制**: 只能使用下面列出的知识文件路径,不得自行编造

可用知识文件列表（仅限以下文件）:
  - D:\projects\HelloAgents-main\knowledge\syntax.md (ArkTS基础语法)
  - D:\projects\HelloAgents-main\knowledge\data.md (数据库相关)

规划原则:
  1. **避免过度拆分**: 相关的代码应该合并到一个步骤中
  2. **功能聚合**: 同类型的方法可以放在一个步骤中一起翻译
  3. **依赖优先**: 被调用的函数应该先于调用它的函数翻译
  4. **协同翻译**: XML和Java必须协同分析,UI组件在build()中统一处理
  5. **步骤精简**: 总步骤数控制在3-8步,避免超过10步
  6. **步骤描述简洁**: 步骤描述只说明要翻译什么内容,不说明具体翻译方法或API转换细节

步骤描述要求:
  - ❌ 不好的描述(包含具体转换细节): "翻译getSimpleTime方法，将Java的SimpleDateFormat和Calendar转换为ArkTS的DateTime和TimeUtil"
  - ✅ 好的描述(只说明翻译内容): "翻译getSimpleTime方法"
  - ❌ 不好的描述(过于详细): "翻译XML布局中的LinearLayout为Column，TextView为Text"
  - ✅ 好的描述(简洁): "翻译UI布局相关代码"

步骤合并示例:
  ❌ 不好的拆分(步骤过多):
    - 步骤1: 翻译import
    - 步骤2: 翻译类声明
    - 步骤3: 翻译常量TAG
    - 步骤4: 翻译变量mContext
    - 步骤5: 翻译变量mData
    - 步骤6: 翻译方法getData()
    - 步骤7: 翻译方法setData()
    - ...
  
  ✅ 好的拆分(步骤合并):
    - 步骤1: 翻译导入语句和类声明
    - 步骤2: 翻译成员变量和常量
    - 步骤3: 翻译数据处理相关方法
    - 步骤4: 翻译UI初始化和更新方法

注意事项:
  1. 支持类(工具类/数据模型)不需要详细拆分,3-4步即可完成
  2. 交互类(Activity/Fragment/Adapter)应控制在5-8步以内
  3. 步骤描述要简洁明确,只说明要翻译的内容,不包含具体转换细节
  4. 将Java代码和XML代码翻译为一个对应的ArkTS代码文件(.ets)
  5. 知识文件列表只能从"可用知识文件列表"中选择,不能添加其他路径
  6. 知识文件数量要合理,通常1-3个,最多不超过3个
  
不应出现以下计划:
  - 翻译Java包声明
  - 翻译Java代码注释
  - 将XML和Java分成两个独立阶段翻译
  - 每个import一个步骤
  - 每个变量一个步骤
  - 每个方法一个步骤
  - 引用不在"可用知识文件列表"中的路径
  - 编造不存在的知识文件路径
  - 步骤描述中包含具体的API转换细节

输出格式:
你的输出必须是有效的JSON对象,包含两个字段:
1. "plan": 字符串数组,包含翻译步骤计划
2. "knowledge_files": 字符串数组,包含需要参考的知识文件路径（必须从上面的"可用知识文件列表"中选择）

输出示例(交互类):
{{
  "plan": [
    "步骤1: 翻译导入语句和类声明",
    "步骤2: 翻译状态变量和成员变量",
    "步骤3: 翻译数据处理和工具方法",
    "步骤4: 翻译UI布局代码",
    "步骤5: 翻译事件处理和生命周期方法"
  ],
  "knowledge_files": [
    "D:\projects\HelloAgents-main\knowledge\syntax.md "
  ]
}}

输出示例(支持类):
{{
  "plan": [
    "步骤1: 翻译导入语句和类声明",
    "步骤2: 翻译常量和静态变量",
    "步骤3: 翻译工具方法"
  ],
  "knowledge_files": [
    "D:\projects\HelloAgents-main\knowledge\syntax.md "
  ]
}}

重要提示: 
1. knowledge_files数组中的所有路径必须严格来自"可用知识文件列表",不能包含其他路径。如果代码不需要参考知识文件,可以返回空数组[]。
2. 步骤描述要简洁,只说明翻译什么内容,不包含具体API转换细节或实现方法。
"""

# 默认执行器提示词模板
# 执行器提示词模板
DEFAULT_EXECUTOR_PROMPT = """
你是一位顶级的Java到ArkTS代码翻译专家。你的任务是严格按照给定的翻译计划，逐步将Java代码翻译成ArkTS代码。
你将收到完整的Java代码、XML代码(可能为空)、已完成的翻译步骤及其结果、以及当前需要执行的翻译步骤。
请你专注于翻译"当前步骤"中的代码片段，并在现有ArkTS代码基础上增量添加翻译结果。

# 相关依赖代码
{dependency}

# 相关知识
{rule_map}

# 完整Java代码:
{java_code}

# 完整XML代码(可能为空):
{xml_code}

# 完整计划:
{plan}

# 历史步骤与结果:
{history}

# 当前已累积的ArkTS代码:
{current_accumulated_code}

# 当前步骤:
{current_step}

---

## 翻译要求

### 代码翻译规则
  1. 在当前已累积的ArkTS代码基础上，添加本步骤翻译的代码
  2. 如果是import语句，追加到已有import区域
  3. 如果是类成员、方法，添加到类体内合适位置
  4. 如果翻译build()方法，需要同时处理XML布局和Java逻辑
  5. 保持代码格式整洁，正确缩进
  6. 如果当前累积代码为空，直接输出本步骤翻译结果
  7. 严格按照计划步骤一步步翻译
  8. 所有翻译结果应合并到同一个.ets文件中
  9. 当前步骤可能包含多个方法或多个变量,请全部翻译完成


### 代码结构规范
  1. **纯Java类翻译**:
     - 导入语句在最前
     - 类声明和装饰器(@Component/@Observed等)
     - 静态常量
     - @State/@Prop状态变量
     - 普通成员变量
     - 构造函数/初始化方法
     - 业务方法
     - 生命周期方法

  2. **功能组件翻译**:
     - 导入语句
     - @Component装饰器和类声明
     - @State/@Prop状态变量
     - 普通成员变量
     - 业务逻辑方法
     - build()方法(UI构建)
     - 事件处理方法
     - 生命周期方法

### 注意事项
  1. 确保语法正确,符合ArkTS规范
  2. 保持原有业务逻辑不变
  3. XML布局必须在build()方法中转换为声明式UI
  4. 事件监听器需要正确绑定到UI组件
  5. 数据绑定需要使用@State等装饰器
  6. 所有资源引用自行填充

---

## 输出格式

转义规则：
- 换行符：\\n（两个反斜杠+n）
- 制表符：\\t（两个反斜杠+t）
- 反斜杠：\\\\（四个反斜杠）
- 双引号：\\"（两个反斜杠+"）

请严格按照以下python格式输出:
```python
{{
  "code": "翻译所得的完整ArkTS代码",
  "address": "代码存储的标准HarmonyOS路径(如HarmonyProject/entry/src/main/ets/pages/MainPage.ets)，
  鸿蒙项目根目录一定为HarmonyProject，必须输出该项即使代码部分为空"
}}
```
"""


class Planner:
    """规划器 - 负责将复杂问题分解为简单步骤"""

    def __init__(self, llm_client: HelloAgentsLLM, prompt_template: Optional[str] = None, ):
        self.llm_client = llm_client
        self.prompt_template = prompt_template if prompt_template else DEFAULT_PLANNER_PROMPT

    def plan(self, java_code: str, **kwargs) -> Dict:
        """
        生成执行计划

        Args:
            java_code: 完整Java代码
            **kwargs: LLM调用参数

        Returns:
            步骤列表
        """
        xml_code = kwargs.get('xml_code', "")
        prompt = self.prompt_template.format(java_code=java_code, xml_code=xml_code)
        messages = [{"role": "user", "content": prompt}]
        print("规划器提示词如下：")
        print(prompt)

        print("--- 正在生成计划 ---")
        response_text = self.llm_client.invoke(messages) or ""
        print(f"✅ 计划已生成:\n{response_text}")

        try:
            plan_str = response_text.split("```json")[1].split("```")[0].strip() \
                if "```json" in response_text else response_text.strip()
            plan = ast.literal_eval(plan_str)
            return plan if isinstance(plan, dict) else {}
        except (ValueError, SyntaxError, IndexError) as e:
            print(f"❌ 解析计划时出错: {e}")
            print(f"原始响应: {response_text}")
            return {}
        except Exception as e:
            print(f"❌ 解析计划时发生未知错误: {e}")
            return {}


class Executor:
    """执行器 - 负责按计划逐步执行"""

    def __init__(self,
                 llm_client: HelloAgentsLLM,
                 prompt_template: Optional[str] = None
                 ):
        self.llm_client = llm_client
        self.prompt_template = prompt_template if prompt_template else DEFAULT_EXECUTOR_PROMPT
        self.accumulated_arkts_code = ""  # 累积的ArkTS代码

    def execute(self, java_code: str, plan: List[str], **kwargs) -> str:
        """
        按计划执行任务

        Args:
            java_code: 完整Java代码
            plan: 执行计划
            **kwargs: LLM调用参数

        Returns:
            最终答案
        """
        history = ""

        print("\n--- 正在执行计划 ---")
        for i, step in enumerate(plan, 1):
            print(f"\n-> 正在执行步骤 {i}/{len(plan)}: {step}")
            prompt = self.prompt_template.format(
                dependency = kwargs.get("dependency", ""),
                rule_map = kwargs.get("rule_map", ""),
                java_code = java_code,
                xml_code = kwargs.get("xml_code", ""),
                plan=plan,
                history=history if history else "无",
                current_accumulated_code=self.accumulated_arkts_code if self.accumulated_arkts_code else "无",
                current_step=step
            )
            messages = [{"role": "user", "content": prompt}]
            print("翻译器提示词如下:")
            print(prompt)
            response_text = self.llm_client.invoke(messages) or ""
            print(f'步骤{i}翻译所得代码：')
            print(response_text)
            # 更新累积的ArkTS代码（处理字典格式）
            code_str = response_text.split("```python")[1].split("```")[0].strip() \
                if "```python" in response_text else response_text.strip()

            try:
                # 尝试用JSON解析
                result = json.loads(code_str)
            except json.JSONDecodeError:
                # JSON解析失败，尝试用Python字面量解析
                try:
                    result = ast.literal_eval(code_str)
                except Exception as e:
                    print(f"❌ 解析失败: {e}")
                    result = None

            self.accumulated_arkts_code = result.get("code", "")
            print('积累所得代码如下：')
            print(self.accumulated_arkts_code)
            self.code_address = result.get("address", "")

            history += f"步骤 {i}: {step}\n结果: {response_text}\n\n"
            print(f"✅ 步骤 {i} 已完成")

        return json.dumps({
            "code": self.accumulated_arkts_code,
            "address": self.code_address
        }, ensure_ascii=False)

    def _extract_code_dict(self, text: str) -> dict:
        """提取JSON字典"""
        import json

        try:
            result = json.loads(text.strip())
            return result
        except:
            return {"code": "", "address": ""}


class PlanAndSolveAgent(Agent):
    """
    Plan and Solve Agent - 分解规划与逐步执行的智能体

    这个Agent能够：
    1. 将复杂翻译问题分解为简单步骤
    2. 按照计划逐步执行，必要时查询RAG获取相关知识
    3. 维护执行历史和上下文
    4. 得出最终答案
    """

    def __init__(
            self,
            name: str,
            llm: HelloAgentsLLM,
            system_prompt: Optional[str] = None,
            config: Optional[Config] = None,
            custom_prompts: Optional[Dict[str, str]] = None
    ):
        """
        初始化PlanAndSolveAgent

        Args:
            name: Agent名称
            llm: LLM实例
            system_prompt: 系统提示词
            config: 配置对象
            custom_prompts: 自定义提示词模板 {"planner": "", "executor": ""}
        """
        super().__init__(name, llm, system_prompt, config)

        if custom_prompts:
            planner_prompt = custom_prompts.get("planner")
            executor_prompt = custom_prompts.get("executor")
        else:
            planner_prompt = None
            executor_prompt = None

        self.planner = Planner(self.llm, planner_prompt)
        self.executor = Executor(self.llm, executor_prompt)

    def run(self, java_code: str, **kwargs) -> str:
        """
        运行Plan and Solve Agent

        Args:
            java_code: 要翻译的java代码
            **kwargs: 其他参数

        Returns:
            最终答案
        """
        print(f"\n🤖 {self.name} 开始翻译代码: {java_code}")

        dependency = kwargs.get("dependency", "")
        xml_code = kwargs.get("xml_code", "")

        plan_dict = self.planner.plan(java_code, xml_code=xml_code)
        plan = plan_dict.get("plan", [])
        rule_paths = plan_dict.get("knowledge_files", "")

        rule_map = ""
        for rule_path in rule_paths:
            with open(rule_path, "r", encoding="utf-8") as f:
                rule_map += f.read()



        if not plan:
            final_answer = "无法生成有效的行动计划，任务终止。"
            print(f"\n--- 任务终止 ---\n{final_answer}")

            self.add_message(Message(java_code, "user"))
            self.add_message(Message(final_answer, "assistant"))

            return final_answer

        final_answer = self.executor.execute(java_code, plan, rule_map=rule_map, dependency=dependency, xml_code=xml_code)

        self.add_message(Message(java_code, "user"))
        self.add_message(Message(final_answer, "assistant"))

        return final_answer
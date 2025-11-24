from dotenv import load_dotenv
from hello_agents.agents.java_translate_agent import *
# 测试示例
if __name__ == "__main__":
    java_path = r"D:\projects\diary-1.0.1\app\src\main\java\com\app\diary\bean\BaseBean.java"
    with open(java_path, 'r', encoding='utf-8') as f:
        java_code = f.read()
    # 加载环境变量
    load_dotenv()
    # 创建LLM实例 - 框架自动检测provider
    llm = HelloAgentsLLM()
    pe = PlanAndSolveAgent(name="规划与解决智能体", llm=llm)
    response = pe.run(java_code, rule_path='JavaToArkTS.md')
    print(response)


from dotenv import load_dotenv
from hello_agents import HelloAgentsLLM
from hello_agents.agents.dependency_analyze_agent import DependencyAnalyzeAgent
from utils import get_project_structure

# 测试示例
if __name__ == "__main__":
    java_path = "D:\projects\\uitranslate\diary-1.0.1\\app\src\main\java\com\\app\diary\\ui\DiaryBrowseActivity.java"
    with open(java_path, 'r', encoding='utf-8') as f:
        java_code = f.read()
    tree = get_project_structure('D:\projects\\uitranslate\diary-1.0.1')
    input_text = f"{tree}|||{java_code}"
    # 加载环境变量
    load_dotenv()
    # 创建LLM实例 - 框架自动检测provider
    llm = HelloAgentsLLM()
    agent = DependencyAnalyzeAgent(
        name = "Java依赖分析",
        llm = llm,
    )

    paths = agent.analyze_dependencies(input_text)
    for path in paths:
        print(path)

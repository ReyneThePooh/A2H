from dotenv import load_dotenv
from hello_agents import HelloAgentsLLM
from hello_agents.agents.xml_layout_analyze_agent import XMLLayoutAnalyzeAgent
from utils import get_project_structure

# 测试示例
if __name__ == "__main__":
    java_path = r"D:\projects\diary-1.0.1\app\src\main\java\com\app\diary\ui\DiaryBrowseActivity.java"
    with open(java_path, 'r', encoding='utf-8') as f:
        java_code = f.read()
    # 加载环境变量
    load_dotenv()
    # 创建LLM实例 - 框架自动检测provider
    llm = HelloAgentsLLM()
    project_root = r'D:\projects\diary-1.0.1'
    source_context = get_project_structure(project_root)
    input_text = f"{source_context}|||{java_code}"
    xml_agent = XMLLayoutAnalyzeAgent(
        name="布局分析",
        llm=llm,
    )
    xml_paths = xml_agent.analyze_layouts(
        input_text
    )
    for p in xml_paths:
        print(p)

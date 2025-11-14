import os
from dotenv import load_dotenv
from hello_agents import HelloAgentsLLM
from hello_agents.agents.dependency_analyze_agent import DependencyAnalyzeAgent

class TopologyGraphExtractor:
    def __init__(self, project_path):
        """
        初始化拓扑图提取器

        Args:
            project_path: Android项目根路径
        """
        self.project_path = os.path.abspath(project_path) # 项目根目录
        self.topology_graph = {} # 拓扑图节点
        self.java_paths = [] # 存储java文件路径

    def scan_project(self):
        """
        扫描Android项目，收集所有Java文件
        """
        print(f"开始扫描项目: {self.project_path}")

        # 扫描项目中的所有Java文件
        for root, _, files in os.walk(self.project_path):
            # 跳过构建目录
            if 'build' in root or '.git' in root:
                continue

            for file in files:
                if file.endswith('.java'):
                    java_path = os.path.join(root, file)
                    # 将单斜杠替换为双斜杠
                    java_path = java_path.replace('/', '\\')
                    self.java_paths.append(java_path)

        print(f"找到 {len(self.java_paths)} 个Java文件")

if __name__ == "__main__":
    project_dir = 'D:\projects\\uitranslate\diary-1.0.1'
    extractor = TopologyGraphExtractor(project_dir)
    extractor.scan_project()
    load_dotenv()
    llm = HelloAgentsLLM()
    agent = DependencyAnalyzeAgent(
        name="Java依赖分析",
        llm=llm,
    )

    for java_path in extractor.java_paths:
        with open(java_path, 'r', encoding='utf-8') as f:
            java_code = f.read()
        input_text = f"{java_path}|||{java_code}"
        paths = agent.analyze_dependencies(input_text)
        print(java_path + ":")
        print(paths)

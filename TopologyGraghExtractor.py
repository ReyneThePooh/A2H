import os
from dotenv import load_dotenv
from hello_agents import HelloAgentsLLM
from hello_agents.agents.dependency_analyze_agent import DependencyAnalyzeAgent
from hello_agents.agents.xml_layout_analyze_agent import XMLLayoutAnalyzeAgent
from utils import get_project_structure

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
        self.xml_to_java_map = {} # XML文件关联的所有XML文件
        self.topology_order = [] # 拓扑图排序

    def _scan_project(self):
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

    def _analyze_dependencies(self):
        """
        分析所有Java文件的依赖关系，包括Java依赖类以及引用XML布局文件
        """
        project_structure = get_project_structure(self.project_path)
        # 加载环境变量
        load_dotenv()
        # 创建LLM实例 - 框架自动检测provider
        llm = HelloAgentsLLM()
        # 布局分析智能体
        xml_agent = XMLLayoutAnalyzeAgent(
            name="布局分析",
            llm=llm,
        )
        # Java分析智能体
        java_agent = DependencyAnalyzeAgent(
            name="Java依赖分析",
            llm=llm,
        )
        for java_path in self.java_paths:
            # 创建节点
            java_path = os.path.abspath(java_path)
            # 节点记录关联XML布局文件以及依赖Java类
            node_info = {
                'xml_paths': [],
                'dependencies': [],
            }
            with open(java_path, 'r', encoding='utf-8') as f:
                java_code = f.read()
            input_text = f"{project_structure}|||{java_code}"
            # 获取Java依赖类
            node_info['dependencies'] = java_agent.analyze_dependencies(input_text)
            # 获取关联XML布局文件
            node_info['xml_paths'] = xml_agent.analyze_layouts(input_text)
            # 添加到拓扑图
            self.topology_graph[java_path] = node_info

            # 构建XML到Java的反向映射
            for xml_path in node_info['xml_paths']:
                if xml_path not in self.xml_to_java_map:
                    self.xml_to_java_map[xml_path] = []
                self.xml_to_java_map[xml_path].append(java_path)

    def build_topological_order(self):
        """
        根据依赖关系构建拓扑排序序列
        使用Kahn算法（基于入度的BFS）

        Returns:
            list: 拓扑排序后的Java文件路径列表，如果存在循环依赖返回None
        """
        from collections import deque, defaultdict
        self._scan_project()
        self._analyze_dependencies()

        # 构建图的邻接表和入度表
        # graph[B] = [A1, A2, ...] 表示 B->A1, B->A2（B被A1、A2依赖）
        graph = defaultdict(list)
        in_degree = defaultdict(int)

        # 初始化所有节点的入度为0
        all_nodes = set(self.topology_graph.keys())
        for node in all_nodes:
            in_degree[node] = 0

        # 构建图
        for java_file, info in self.topology_graph.items():
            dependencies = info['dependencies']
            for dep in dependencies:
                # dep -> java_file (java_file依赖dep)
                if dep in all_nodes:  # 只考虑项目内的依赖
                    graph[dep].append(java_file)
                    in_degree[java_file] += 1

        # Kahn算法：找出所有入度为0的节点
        queue = deque()
        for node in all_nodes:
            if in_degree[node] == 0:
                queue.append(node)

        topological_order = []

        while queue:
            # 取出入度为0的节点
            current = queue.popleft()
            topological_order.append(current)

            # 遍历当前节点的所有邻居
            for neighbor in graph[current]:
                in_degree[neighbor] -= 1
                # 如果邻居入度变为0，加入队列
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        # 检查是否存在循环依赖
        if len(topological_order) != len(all_nodes):
            # 找出循环依赖的节点
            remaining_nodes = all_nodes - set(topological_order)
            print(f"涉及循环依赖的节点: {remaining_nodes}")
            return None

        # 保存拓扑序列
        self.topological_order = topological_order
        return topological_order

if __name__ == "__main__":
    project_dir = 'D:\projects\\uitranslate\diary-1.0.1'
    extractor = TopologyGraphExtractor(project_dir)
    order = extractor.build_topological_order()
    for node in order:
        deps = extractor.topology_graph[node]['dependencies']
        print(f"{node}: {deps}")



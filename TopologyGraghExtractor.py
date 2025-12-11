import os
from dotenv import load_dotenv
from hello_agents import HelloAgentsLLM
from hello_agents.agents.dependency_analyze_agent import DependencyAnalyzeAgent
from hello_agents.agents.interaction_analyzer import InteractionAnalyzeAgent
from hello_agents.agents.resource_analyze_agent import ResourceAnalyzeAgent
from hello_agents.agents.component_extraction import FunctionalUnitAnalyzeAgent

from utils import get_project_structure

class TopologyGraphExtractor:
    def __init__(self, project_path):
        """
        初始化拓扑图提取器

        Args:
            project_path: Android项目根路径
        """
        self.project_path = os.path.abspath(project_path) # 项目根目录
        self.support_java_dependency = {} # support类java依赖
        self.interaction_java_dependency = {} # 交互类java依赖


        self.interaction_java_paths = set() # 存储引用XML文件的Java文件路径
        self.support_java_paths = set() # 存储Java文件路径
        self.xml_paths = set() # 存储XML文件路径
        self.component = [] # 功能单元

        self.topology_order = [] # 拓扑图排序
        self.java_resources = {} # 资源文件
        self.xml_resources = {} # 资源文件
        self.topology_groups = []

    def _scan_project(self):
        """
        扫描Android项目，收集所有Java文件以及XML布局文件
        """
        print(f"开始扫描项目: {self.project_path}")

        # 扫描项目中的所有Java文件和XML文件
        for root, _, files in os.walk(self.project_path):
            # 跳过构建目录
            if 'build' in root or '.git' in root:
                continue

            for file in files:
                file_path = os.path.join(root, file)
                # 将单斜杠替换为双斜杠
                file_path = file_path.replace('/', '\\')

                if file.endswith('.java'):
                    self.support_java_paths.add(file_path)
                elif file.endswith('.xml'):
                    # 可以选择只收集layout目录下的XML文件
                    if 'layout' in root or 'res' in root:
                        self.xml_paths.add(file_path)

        print(f"找到 {len(self.support_java_paths)} 个Java文件")
        print(f"找到 {len(self.xml_paths)} 个XML文件")

    def _select_java(self):
        """
        选择出与交互逻辑有关的Java文件，存储在列表中
        """
        load_dotenv()
        llm = HelloAgentsLLM()
        xml_agent = InteractionAnalyzeAgent(
            name="布局分析",
            llm=llm,
        )

        self.interaction_java_paths = set()

        # 转换为列表以便分批处理
        java_paths_list = list(self.support_java_paths)
        batch_size = 5
        total_batches = (len(java_paths_list) + batch_size - 1) // batch_size

        print(f"开始分析Java文件类别，共 {len(java_paths_list)} 个文件，分 {total_batches} 批处理")

        # 分批处理
        for i in range(0, len(java_paths_list), batch_size):
            batch_paths = java_paths_list[i:i + batch_size]
            batch_num = i // batch_size + 1
            print(f"处理第 {batch_num}/{total_batches} 批...")

            java_dict = {}
            for java_path in batch_paths:
                try:
                    with open(java_path, 'r', encoding='utf-8') as f:
                        java_code = f.read()
                        java_dict[java_path] = java_code
                except Exception as e:
                    print(f"读取文件失败 {java_path}: {e}")
                    continue

            # 将字典转换为字符串传递给agent
            java_dict_str = str(java_dict)

            # 调用agent分析
            try:
                related_paths = xml_agent.analyze_layouts(java_dict_str)
                if related_paths:
                    self.interaction_java_paths.update(related_paths)
            except Exception as e:
                print(f"分析第 {batch_num} 批文件时出错: {e}")
                continue

        # 从原java_paths中移除与交互相关的文件
        self.support_java_paths -= self.interaction_java_paths

        print(f"分析完成: 找到 {len(self.interaction_java_paths)} 个与交互相关的Java文件")
        print(f"剩余 {len(self.support_java_paths)} 个纯Java文件")


    def _component_extraction(self):
        load_dotenv()
        llm = HelloAgentsLLM()
        agent = FunctionalUnitAnalyzeAgent(name='功能模块分析智能体', llm=llm)
        java = {}
        xml = {}
        for java_path in self.interaction_java_paths:
            with open(java_path, 'r', encoding='utf-8') as f:
                java[java_path] = f.read()
        for xml_path in self.xml_paths:
            with open(xml_path, 'r', encoding='utf-8') as f:
                xml[xml_path] = f.read()
        self.component = agent.analyze_units(str(java), str(xml))

    def _analyze_dependencies(self):
        """
        分析Java文件以及Component的Java依赖关系
        """
        project_structure = get_project_structure(self.project_path)
        load_dotenv()
        java_llm = HelloAgentsLLM()
        java_agent = DependencyAnalyzeAgent(
            name="Java依赖分析",
            llm=java_llm,
        )

        # 收集support类java依赖的java类
        for java_path in self.support_java_paths:
            with open(java_path, 'r', encoding='utf-8') as f:
                java_code = f.read()
            self.support_java_dependency[java_path] = set(java_agent.analyze_dependencies(project_structure, java_code=java_code))
            self.support_java_dependency[java_path].discard(java_path)

        # 收集交互类java的依赖类（排除交互类java）
        for java_path in self.interaction_java_paths:
            with open(java_path, 'r', encoding='utf-8') as f:
                java_code = f.read()
            self.interaction_java_dependency[java_path] = set(java_agent.analyze_dependencies(project_structure, java_code=java_code))
            self.interaction_java_dependency[java_path] -= self.interaction_java_paths

    def _analyze_resource(self):
        """
        分析Java文件或XML文件依赖的资源文件
        """
        res_llm = HelloAgentsLLM()
        agent = ResourceAnalyzeAgent(name='资源提取', llm=res_llm)
        project_structure = get_project_structure(self.project_path)

        for java_path in self.support_java_paths:
            with open(java_path, 'r', encoding='utf-8') as f:
                java_code = f.read()
            java_resource = agent.analyze_resources(project_structure, java_code=java_code)
            self.java_resources[java_path] = java_resource

        for xml_path in self.xml_paths:
            with open(xml_path, 'r', encoding='utf-8') as f:
                xml_code = f.read()
            xml_resource = agent.analyze_resources(project_structure, xml_code=xml_code)
            self.xml_resources[xml_path] = xml_resource

    def build_topological_order(self):
        """
        根据依赖关系构建拓扑排序序列
        使用Kahn算法（基于入度的BFS）

        Returns:
            list: 拓扑排序后的Java文件路径列表，如果存在循环依赖返回None
        """
        from collections import deque, defaultdict

        self._scan_project()
        self._select_java()
        self._component_extraction()
        self._analyze_dependencies()

        all_nodes = set(self.support_java_dependency.keys())

        deps_adj = {}
        for a, deps in self.support_java_dependency.items():
            deps_adj[a] = {b for b in deps if b in all_nodes}

        index_counter = [0]
        stack = []
        on_stack = set()
        indices = {}
        lowlink = {}
        sccs = []

        def strongconnect(v):
            indices[v] = index_counter[0]
            lowlink[v] = index_counter[0]
            index_counter[0] += 1
            stack.append(v)
            on_stack.add(v)
            for w in deps_adj.get(v, set()):
                if w not in indices:
                    strongconnect(w)
                    lowlink[v] = lowlink[v] if lowlink[v] < lowlink[w] else lowlink[w]
                elif w in on_stack:
                    lowlink[v] = lowlink[v] if lowlink[v] < indices[w] else indices[w]
            if lowlink[v] == indices[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                sccs.append(comp)

        for node in all_nodes:
            if node not in indices:
                strongconnect(node)

        comp_id = {}
        for i, comp in enumerate(sccs):
            for n in comp:
                comp_id[n] = i

        comp_graph = defaultdict(set)
        comp_in_degree = defaultdict(int)
        for i in range(len(sccs)):
            comp_in_degree[i] = 0
        for a in all_nodes:
            ca = comp_id[a]
            for b in deps_adj.get(a, set()):
                cb = comp_id[b]
                if ca != cb and ca not in comp_graph[cb]:
                    comp_graph[cb].add(ca)
                    comp_in_degree[ca] += 1

        comp_queue = deque()
        for i in range(len(sccs)):
            if comp_in_degree[i] == 0:
                comp_queue.append(i)

        ordered_comps = []
        while comp_queue:
            ci = comp_queue.popleft()
            ordered_comps.append(ci)
            for nj in comp_graph[ci]:
                comp_in_degree[nj] -= 1
                if comp_in_degree[nj] == 0:
                    comp_queue.append(nj)

        groups_ordered = []
        for ci in ordered_comps:
            group = sorted(sccs[ci], key=lambda n: (len(deps_adj.get(n, set())), n))
            groups_ordered.append(group)

        flattened = []
        for g in groups_ordered:
            flattened.extend(g)

        self.topology_groups = groups_ordered
        self.topological_order = flattened
        cyclic_groups = sum(1 for g in groups_ordered if len(g) > 1)
        if cyclic_groups > 0:
            print(f"⚠️ 检测到循环依赖，合并为 {cyclic_groups} 个强连通分量")
        print(f"✅ 拓扑排序成功，共 {len(flattened)} 个支持类型Java文件")
        return flattened

if __name__ == "__main__":
    project_dir = 'D:\projects\\uitranslate\diary-1.0.1'
    extractor = TopologyGraphExtractor(project_dir)
    extractor._scan_project()
    extractor._select_java()
    # extractor._component_extraction()
    # extractor._analyze_dependencies()
    #
    # print(type(extractor.support_java_paths)) # set
    # print(type(extractor.component)) # list
    # print(type(extractor.java_node)) # dict
    #
    print(extractor.support_java_paths)
    print(extractor.interaction_java_paths)
    # print(extractor.component)
    # print(extractor.java_node)

    # order = extractor.build_topological_order()
    # for java_path in order:
    #     print(java_path + ': ' + str(extractor.java_node[java_path]))




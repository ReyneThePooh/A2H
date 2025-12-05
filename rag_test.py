from hello_agents import SimpleAgent, HelloAgentsLLM, ToolRegistry
from hello_agents.tools import RAGTool

# 创建具有RAG能力的Agent
# llm = HelloAgentsLLM()
# agent = SimpleAgent(name="知识助手", llm=llm)
#
# # 创建RAG工具
# rag_tool = RAGTool(
#     knowledge_base_path="./knowledge_base",
#     collection_name="test_collection",
#     rag_namespace="test"
# )
#
# tool_registry = ToolRegistry()
# tool_registry.register_tool(rag_tool)
# agent.tool_registry = tool_registry
#
# # 体验RAG功能
# # 添加第一个知识
# result1 = rag_tool.run({"action":"add_text",
#     "text":"Python是一种高级编程语言，由Guido van Rossum于1991年首次发布。Python的设计哲学强调代码的可读性和简洁的语法。",
#     "document_id":"python_intro"}
# )
# print(f"知识1: {result1}")
#
# # 添加第二个知识
# result2 = rag_tool.run({"action":"add_text",
#     "text":"机器学习是人工智能的一个分支，通过算法让计算机从数据中学习模式。主要包括监督学习、无监督学习和强化学习三种类型。",
#     "document_id":"ml_basics"}
# )
# print(f"知识2: {result2}")
#
# # 添加第三个知识
# result3 = rag_tool.run({"action":"add_text",
#     "text":"RAG（检索增强生成）是一种结合信息检索和文本生成的AI技术。它通过检索相关知识来增强大语言模型的生成能力。",
#     "document_id":"rag_concept"}
# )
# print(f"知识3: {result3}")
#
# print("\n=== 搜索知识 ===")
# result = rag_tool.run({"action":"search",
#     "query":"Python编程语言的历史",
#     "limit":3,
#     "min_score":0.1}
# )
# print(result)

import os


def get_all_md_paths(folder_path: str) -> list[str]:
    """
    递归遍历指定文件夹，返回所有 .md 文件的绝对路径列表
    :param folder_path: 目标文件夹路径（支持相对路径/绝对路径，Windows/Linux 通用）
    :return: 所有 md 文件的绝对路径列表（无 md 文件时返回空列表）
    """
    md_paths = []
    # 处理 Windows 路径转义，统一转为绝对路径
    folder_path = os.path.abspath(folder_path)

    # 检查文件夹是否存在
    if not os.path.exists(folder_path):
        print(f"警告：文件夹不存在 -> {folder_path}")
        return md_paths
    if not os.path.isdir(folder_path):
        print(f"警告：不是有效文件夹 -> {folder_path}")
        return md_paths

    # 递归遍历所有文件
    for root, _, files in os.walk(folder_path):
        for file in files:
            # 匹配 .md 和 .MD 后缀
            if file.lower().endswith(".md"):
                # 拼接绝对路径并添加到列表
                md_path = os.path.abspath(os.path.join(root, file))
                md_paths.append(md_path)

    return md_paths

if __name__ == '__main__':
    rag_tool = RAGTool(
        knowledge_base_path="./knowledge_base_path",
        collection_name="ArkTS API collection",
        rag_namespace="ArkTS API",
    )

    tool_registry = ToolRegistry()
    tool_registry.register_tool(rag_tool)

    TARGET_FOLDER = r"D:\resources\test"
    # file_path = r"D:\resources\test\apis-arkui\arkts-apis-window-i.md"
    # for i in range(10):
    #     rag_tool.add_document(file_path)

    # rag_tool.add_documents_batch(get_all_md_paths(TARGET_FOLDER))

    result = rag_tool.run({"action":"ask",
        "query":"ArkTS代码如何操作数据库，提供具体实例",
        "limit":20
    }
    )
    print(result)
import json
import os
from pathlib import Path

from TopologyGraghExtractor import *
from hello_agents.agents.java_translate_agent import *
from hello_agents.agents.code_reflection_agent import *
from hello_agents.core.path_config import ANDROID_PROJECT_DIR, HARMONY_SOURCE_PROJECT_DIR


def resolve_output_path(address):
    path = Path(address)
    if path.is_absolute():
        return path

    parts = path.parts
    if parts and parts[0] == HARMONY_SOURCE_PROJECT_DIR.name:
        path = Path(*parts[1:])
    return HARMONY_SOURCE_PROJECT_DIR / path

if __name__ == '__main__':
    project_dir = str(ANDROID_PROJECT_DIR)
    extractor = TopologyGraphExtractor(project_dir)
    order = extractor.build_topological_order() # 构建拓扑排序
    support_java_dependency = extractor.support_java_dependency
    component = extractor.component
    interaction_java_dependency = extractor.interaction_java_dependency
    print(order) # list
    print(extractor.support_java_paths)
    print(extractor.interaction_java_paths)
    print(extractor.component)
    print(extractor.support_java_dependency)
    print(extractor.interaction_java_dependency)

    java_to_ets = {}
    for java_path in order:
        llm = HelloAgentsLLM()
        pe = PlanAndSolveAgent(name='翻译', llm=llm)
        try:
            print(f"\n{'=' * 60}")
            print(f"正在处理: {java_path}")
            print(f"{'=' * 60}")

            # 读取Java文件
            try:
                with open(java_path, "r", encoding="utf-8") as f:
                    java_code = f.read()
            except Exception as e:
                print(f"❌ 读取Java文件失败: {e}")
                continue

            # 收集依赖（包含路径和代码）
            dependency = ''
            for dep in support_java_dependency.get(java_path, set()):
                if dep in java_to_ets:
                    try:
                        dep_result = java_to_ets[dep]
                        dependency += f"// ===== 依赖文件: {dep} =====\n"
                        dependency += f"// 翻译后路径: {dep_result.get('address', '')}\n"
                        dependency += dep_result.get('code', '') + '\n\n'
                    except Exception as e:
                        print(f"⚠️ 处理依赖失败 {dep}: {e}")

            # 调用翻译
            try:
                response = pe.run(java_code, dependency=dependency)
                print('响应结果如下')
                print(response)
            except Exception as e:
                print(f"❌ 翻译失败: {e}")
                continue


            if len(dependency) > 0:
                print("依赖文件如下")
                print(dependency[:500] + '...' if len(dependency) > 500 else dependency)

            # 解析JSON字符串
            try:
                result = json.loads(response)
                code = result.get("code", "")
                address = result.get("address", "")
                if not code or not address:
                    print(f"⚠️ 返回结果缺少code或address字段")
                    continue

            except json.JSONDecodeError as e:
                print(f"❌ JSON解析失败: {e}")
                print(f"响应内容: {response[:500]}...")
                continue

            # 创建目录并写入文件
            try:
                target_path = resolve_output_path(address)
                directory = os.path.dirname(target_path)
                if directory and not os.path.exists(directory):
                    os.makedirs(directory, exist_ok=True)
                    print(f"✅ 创建目录: {directory}")

                with open(target_path, 'w', encoding='utf-8') as f:
                    f.write(code)

                print(f"✅ 代码已写入: {target_path}")
                result["address"] = str(target_path)
                java_to_ets[java_path] = result

            except Exception as e:
                print(f"❌ 写入文件失败 {address}: {e}")
                continue

        except Exception as e:
            print(f"❌ 处理Java文件时发生未知错误: {e}")
            import traceback

            traceback.print_exc()
            continue


    print(f"\n{'=' * 60}")
    print("开始处理功能单位")
    print(f"{'=' * 60}\n")

    for unit in component:
        llm = HelloAgentsLLM()
        pe = PlanAndSolveAgent(name='翻译', llm=llm)
        dep = unit.get('dependency', '')
        xml_files = unit.get('xml_files', [])
        java_files = unit.get('java_files', [])

        try:
            print(f"\n{'=' * 60}")
            print(f"正在处理功能单元: {dep}")
            print(f"{'=' * 60}")

            # 读取XML文件
            try:
                xml_code = ''
                for xml_file in xml_files:
                    with open(xml_file, "r", encoding="utf-8") as f:
                        xml_code += f"// ===== XML文件: {xml_file} =====\n"
                        xml_code += f.read() + '\n\n'
            except Exception as e:
                print(f"❌ 读取XML文件失败: {e}")
                continue

            java_code = ''
            dependency = ''
            res_set = set()
            dep_set = set()

            # 处理每个关联的Java文件
            for java_file in java_files:
                try:
                    # 读取Java代码
                    with open(java_file, "r", encoding="utf-8") as f:
                        java_code += f"// ===== Java文件: {java_file} =====\n"
                        java_code += f.read() + '\n\n'

                    for dep in interaction_java_dependency[java_file]:
                        if dep in java_to_ets:
                            try:
                                dep_result = java_to_ets[dep]
                                dependency += f"// ===== 依赖文件: {dep} =====\n"
                                dependency += f"// 翻译后路径: {dep_result.get('address', '')}\n"
                                dependency += dep_result.get('code', '') + '\n\n'
                            except Exception as e:
                                print(f"⚠️ 处理依赖失败 {dep}: {e}")
                except Exception as e:
                    print(f"⚠️ 处理关联Java文件失败 {java_file}: {e}")
                    continue

            # 调用翻译
            try:
                response = pe.run(java_code, xml_code=xml_code, dependency=dependency)
            except Exception as e:
                print(f"❌ 翻译失败: {e}")
                continue

            # 解析JSON字符串
            try:
                result = json.loads(response)
                code = result.get("code", "")
                address = result.get("address", "")

                if not code or not address:
                    print(f"⚠️ 返回结果缺少code或address字段")
                    continue

            except json.JSONDecodeError as e:
                print(f"❌ JSON解析失败: {e}")
                print(f"响应内容: {response[:500]}...")
                continue

            # 创建目录并写入文件
            try:
                target_path = resolve_output_path(address)
                directory = os.path.dirname(target_path)
                if directory and not os.path.exists(directory):
                    os.makedirs(directory, exist_ok=True)
                    print(f"✅ 创建目录: {directory}")

                with open(target_path, 'w', encoding='utf-8') as f:
                    f.write(code)

                print(f"✅ 代码已写入: {target_path}")

            except Exception as e:
                print(f"❌ 写入文件失败 {address}: {e}")
                continue

        except Exception as e:
            print(f"❌ 处理XML文件时发生未知错误: {e}")
            import traceback

            traceback.print_exc()
            continue

    print(f"\n{'=' * 60}")
    print("所有文件处理完成")
    print(f"✅ 成功翻译的Java文件数: {len(java_to_ets)}")
    print(f"{'=' * 60}")

    load_dotenv()
    config = Config.from_env()
    llm = HelloAgentsLLM()

    agent = HarmonyFeedbackAgent(
        llm=llm,
        config=config,
        max_iterations=3
    )

    result = agent.run()

    print("\n" + "=" * 70)
    print("🎯 最终结果")
    print("=" * 70)

    if result.get('success'):
        print("🎉 所有错误已修复！")
        print(f"总共修复了 {result.get('total_errors_fixed')} 个错误")
        print(f"迭代次数: {result.get('iterations')}")
    else:
        print(f"⚠️ 仍有 {result.get('remaining_errors_count', 0)} 个错误未解决")
        print("建议：检查剩余错误，可能需要人工介入")
        if result.get('final_errors'):
            print("\n剩余错误:")
            for i, err in enumerate(result['final_errors'][:3], 1):
                print(f"  {i}. {err.get('file')} L{err.get('line')}: {err.get('message')}")


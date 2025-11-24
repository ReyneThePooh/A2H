from TopologyGraghExtractor import *
from hello_agents.agents.java_translate_agent import *
from hello_agents.agents.resource_analyze_agent import *


if __name__ == '__main__':
    # project_dir = 'D:\projects\\uitranslate\diary-1.0.1'
    # extractor = TopologyGraphExtractor(project_dir)
    # order = extractor.build_topological_order() # 构建拓扑排序
    #
    # print(order) # list
    # print(extractor.support_java_paths)
    # print(extractor.interaction_java_paths)
    # print(extractor.component)
    # print(extractor.support_java_dependency)
    # print(extractor.interaction_java_dependency)

    order = ['D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\utils\\TimeUtils.java', 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\bean\\BaseBean.java', 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\utils\\SizeUtils.java', 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\db\\DbHelper.java', 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\bean\\Diary.java', 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\data\\DiaryDataSource.java', 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\data\\impl\\DiaryDataSourceImpl.java', 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\Mapp.java', 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\utils\\AppUtils.java', 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\utils\\ToastUtils.java']
    support_java_paths = {'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\Mapp.java', 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\bean\\BaseBean.java', 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\data\\DiaryDataSource.java', 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\utils\\TimeUtils.java', 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\utils\\SizeUtils.java', 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\data\\impl\\DiaryDataSourceImpl.java', 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\db\\DbHelper.java', 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\utils\\AppUtils.java', 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\utils\\ToastUtils.java', 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\bean\\Diary.java'}
    interaction_java_paths = {'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\ui\\BaseActivity.java', 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\ui\\MainActivity.java', 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\ui\\DiaryBrowseActivity.java', 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\adapter\\DiaryRecyclerAdapter.java', 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\ui\\DiaryEditActivity.java', 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\ui\\DiaryListActivity.java'}
    component = [{'description': '基础Activity类', 'java_files': ['D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\ui\\BaseActivity.java'], 'xml_files': []}, {'description': '主页面', 'java_files': ['D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\ui\\MainActivity.java'], 'xml_files': ['D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\res\\layout\\activity_main.xml']}, {'description': '日记列表页面', 'java_files': ['D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\ui\\DiaryListActivity.java', 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\adapter\\DiaryRecyclerAdapter.java'], 'xml_files': ['D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\res\\layout\\activity_diary_list.xml', 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\res\\layout\\item_recycler_diary.xml']}, {'description': '日记浏览页面', 'java_files': ['D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\ui\\DiaryBrowseActivity.java'], 'xml_files': ['D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\res\\layout\\activity_diary_browse.xml', 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\res\\menu\\menu_diary_browse.xml']}, {'description': '日记编辑页面', 'java_files': ['D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\ui\\DiaryEditActivity.java'], 'xml_files': ['D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\res\\layout\\activity_diary_edit.xml', 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\res\\menu\\menu_diary_create.xml']}]
    support_java_dependency = {'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\Mapp.java': {'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\db\\DbHelper.java', 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\data\\DiaryDataSource.java', 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\data\\impl\\DiaryDataSourceImpl.java'}, 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\bean\\BaseBean.java': set(), 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\data\\DiaryDataSource.java': {'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\bean\\Diary.java'}, 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\utils\\TimeUtils.java': set(), 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\utils\\SizeUtils.java': set(), 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\data\\impl\\DiaryDataSourceImpl.java': {'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\db\\DbHelper.java', 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\data\\DiaryDataSource.java', 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\bean\\Diary.java'}, 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\db\\DbHelper.java': set(), 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\utils\\AppUtils.java': {'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\Mapp.java'}, 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\utils\\ToastUtils.java': {'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\Mapp.java'}, 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\bean\\Diary.java': {'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\bean\\BaseBean.java'}}
    interaction_java_dependency = {'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\ui\\BaseActivity.java': set(), 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\ui\\MainActivity.java': {'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\utils\\AppUtils.java'}, 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\ui\\DiaryBrowseActivity.java': {'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\Mapp.java', 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\utils\\ToastUtils.java', 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\bean\\Diary.java'}, 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\adapter\\DiaryRecyclerAdapter.java': {'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\utils\\TimeUtils.java', 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\bean\\Diary.java'}, 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\ui\\DiaryEditActivity.java': {'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\bean\\Diary.java', 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\utils\\ToastUtils.java', 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\Mapp.java'}, 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\ui\\DiaryListActivity.java': {'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\Mapp.java', 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\utils\\SizeUtils.java', 'D:\\projects\\uitranslate\\diary-1.0.1\\app\\src\\main\\java\\com\\app\\diary\\bean\\Diary.java'}}

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
                response = pe.run(java_code, dependency=dependency, rule_path='JavaToArkTS.md')
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
                directory = os.path.dirname(address)
                if directory and not os.path.exists(directory):
                    os.makedirs(directory, exist_ok=True)
                    print(f"✅ 创建目录: {directory}")

                with open(address, 'w', encoding='utf-8') as f:
                    f.write(code)

                print(f"✅ 代码已写入: {address}")
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
                response = pe.run(java_code, rule_path='JavaToArkTS.md', xml_code=xml_code, dependency=dependency)
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
                directory = os.path.dirname(address)
                if directory and not os.path.exists(directory):
                    os.makedirs(directory, exist_ok=True)
                    print(f"✅ 创建目录: {directory}")

                with open(address, 'w', encoding='utf-8') as f:
                    f.write(code)

                print(f"✅ 代码已写入: {address}")

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




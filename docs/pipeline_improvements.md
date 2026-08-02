# 翻译顺序确定阶段改进说明

> 分支：`feature/unit-pipeline-improvements`
> 涉及阶段：依赖分析 → 翻译单元（Unit）划分 → 拓扑排序
> 涉及文件：`analyzers/static.py`、`analyzers/tools.py`、`pipeline/static_graph.py`（新增）、`pipeline/order_determiner.py`、`tests/test_static_graph.py`（新增）

## 背景问题

改进前，该阶段"能静态确定的事实交给了不确定的 LLM"：

1. **同包引用完全漏检**。静态依赖只看 `project_imports`，而典型小项目所有类在同一个包下、没有项目内 import，导致静态依赖恒为空，依赖图 100% 靠 LLM 猜。
2. **import 匹配靠文件名后缀**。`_match_file_to_unit` 用 `endswith("类名.java")` 匹配，重名类会绑错，且只处理 `/` 分隔符，Windows 的 `\` 路径会匹配失败。
3. **依赖 prompt 边方向自相矛盾**。prompt 写"键是被依赖的单元名"，代码却按"键依赖值列表"合并——LLM 若照 prompt 理解，输出的边方向全反，拓扑序整体颠倒。
4. **Java↔XML 配对纯靠 LLM**。`setContentView(R.layout.x)` → `layout/x.xml` 这种可静态确定的绑定没有参与 Unit 划分，LLM 可能拆散强耦合对。
5. **循环依赖处理粗糙**。旧 `topological_sort` 把入度无法归零的节点全部堆到最后一层，环内顺序随机，且**依赖环的正常下游节点也跟着被错误堆到最后**。
6. **无校验、无产物**。LLM 输出只查"漏文件"，不查重复分配、绑错、拓扑违规；中间结果不落盘，出问题无法定位。

## 改进内容

### 1. 静态依赖提取增强（`analyzers/static.py`）

`FileSummary` 新增两个零 token 字段：

- `type_references`：通过 javalang AST 提取代码中引用的类型简单名，来源包括字段/参数/返回值/局部变量类型（`ReferenceType`）、首字母大写的静态成员访问与静态方法调用限定名（`DbHelper.TABLE_NAME`、`Utils.format()`）、`Xxx.class` 引用、`extends`/`implements`。**这是覆盖"同包无 import 引用"的关键**。
- `intent_targets`：正则提取 `new Intent(..., Xxx.class)` 与 `setClass(..., Xxx.class)` 的跳转目标类名。

### 2. 确定性静态依赖层（新增 `pipeline/static_graph.py`）

- **`ProjectIndex`**：FQCN → 文件的精确索引；类简单名解析时重名类优先同包；布局名 → 布局文件索引。取代原来的文件名后缀匹配。
- **`norm_path`**：所有路径统一为 `/` 分隔符，Windows 路径不再匹配失败。
- **`build_file_graph`**：文件级依赖图，边全部来自确定性规则——项目内 import（FQCN 精确解析）、同包类型引用、Intent 跳转、Java → 引用的 layout、layout → `<include>` 的子布局。
- **`build_hard_groups`**：union-find 预分组。引用同一布局的 Java 与该布局强制同组，include 的子布局随父布局同组，产出"硬绑定组"。
- **`tarjan_scc` + `layered_topological_sort`**：Tarjan 缩点（迭代实现）后对 DAG 做最长路径分层。环内成员同层并作为 `cycles` 显式返回；环的下游节点正确排在环之后。
- **`validate_plan`**：不变量校验——每个文件恰好属于一个 Unit、依赖边两端存在、任意 Unit 的依赖都在更早的层、硬绑定组未被拆散。
- **`export_artifacts`**：把 units、文件依赖图、unit 依赖、分层、环、违规项落盘为 `.pipeline_cache/translation_plan.json`，并生成 `unit_graph.mmd`（mermaid）供人工核查。

### 3. Unit 划分收紧（`pipeline/order_determiner.py` — `UnitBuilder`）

- prompt 中加入**硬绑定组约束**：组内文件必须同单元，可合并不可拆分。
- LLM 输出后处理三连：
  - `_normalize_sources`：LLM 输出的路径规范化并匹配到真实文件，幻觉路径丢弃并告警；
  - `_repair_units`：去重（一个文件只留在首个单元）、补漏（未覆盖文件单独成单元）、**强制执行硬绑定**（组被拆散时全部移入锚点单元，即组内第一个 Java 文件所在单元）、清理空单元；
  - 兜底 `_fallback_units` 从"每文件一单元"升级为"按硬绑定组成单元 + 剩余单文件单元"。

### 4. Unit 依赖分析重写（`UnitDependencyAnalyzer`）

- 静态第一遍改为**文件级依赖图投影到 Unit 级**，覆盖 import、同包引用、Intent、布局引用；删除按文件名后缀匹配的 `_match_file_to_unit` 和死代码 `_import_to_path`。
- LLM 第二遍降级为**审核 + 补充**：prompt 明确"静态结果不许删、只补隐式协议依赖（共享持久化数据、广播、全局状态）"，并把静态结果直接给 LLM 作基线；去掉了原 prompt 里样例项目专属的假设（"所有类同包"、"MainActivity → HomeActivity"）。
- **修复边方向歧义**：prompt 现在明确"键是依赖方，`"单元A": ["单元B"]` 表示 A 依赖 B、B 先翻译"。
- 支持 `analyze(units, use_llm=False)` 纯静态模式，便于离线测试与调试。

### 5. 拓扑排序重写（`topological_sort`）

- 委托给 `layered_topological_sort`（SCC 缩点 + 最长路径分层），返回值变为 `(layers, cycles)`。
- 层内按"被依赖数多的在前 + 名字典序"排序，串行执行时先翻译更基础的 Unit，结果可复现。
- 检测到环时打印警告并显式返回环成员，供翻译阶段把环内成员放进同一上下文。

### 6. 编排与缓存（`OrderDeterminer.run`）

- 流程从 5 步扩为 6 步：扫描 → 摘要 → **静态依赖图 + 硬绑定分组** → Unit 划分（带约束）→ Unit 依赖（静态投影 + LLM 补充）→ SCC 拓扑排序。
- 结束时运行 `validate_plan` 打印违规项，并 `export_artifacts` 落盘。
- 旧摘要缓存缺少新静态字段时，`_refresh_static_fields` 用零 token 的静态分析补齐并回写缓存，**不需要重新消耗 LLM 生成语义摘要**。

## 验证

测试文件 `tests/test_static_graph.py`（16 个用例，全部通过），使用合成迷你项目（同包、无项目内 import——正是改进前的痛点场景）覆盖：

- 同包类型引用 / 静态成员限定名 / Intent 目标 / 布局引用的提取；
- 文件级依赖图的各类边（含 Windows 反斜杠路径）；
- 硬绑定分组（Activity+布局+include、Adapter+item 布局、无布局类不入组）;
- Tarjan SCC、DAG 分层、"环 + 下游节点"场景（旧实现的 bug 场景）；
- Unit 静态依赖投影（不走 LLM）；
- `_repair_units` 的去重/补漏/硬绑定强制执行、`_normalize_sources` 的路径修复;
- `validate_plan` 对漏文件、拓扑违规、硬绑定拆散的检出。

运行方式（A2H 虚拟环境）：

```powershell
conda activate A2H
python -m pytest tests/test_static_graph.py -v
```

依赖变更：`requirements.txt` 新增 `javalang>=0.13.0`（已装入 A2H 环境）。

## 对下游的影响

- `OrderDeterminer.run()` 返回值不变（`layers, unit_deps`），`pipeline/unit_translator.py` 无需修改。
- `topological_sort` 的返回值从 `layers` 变为 `(layers, cycles)`，仓库内唯一调用方（`OrderDeterminer.run`）已同步更新；如有外部调用需注意。
- 翻译阶段可进一步利用两个新产物：`cycles`（环内成员应共享翻译上下文）和 `.pipeline_cache/translation_plan.json`（可回放、可人工审查的完整计划）。

## 补充：完整工程打包（2026-07-21 新增）

翻译产物现在可以直接套进 DevEco 壳工程模板，产出可构建、可运行的完整鸿蒙工程：

- **`pipeline/project_packager.py`**：复制 `HarmonyTemplate/template` 模板（跳过 build/.hvigor 等缓存）→ 合并翻译产物的 ets/resources（`pages/` 整目录替换，element JSON **键级合并**，保留模板 `module_desc` 等被 `module.json5` 引用的 key）→ 仅把含 `@Entry` 的页面注册进 `main_pages.json` 并同步 `EntryAbility.loadContent` 入口页 → 把 AndroidManifest 的 `uses-permission` 映射为 ohos 权限注入 `module.json5` → 为主流程提供 `hvigorw assembleHap` 构建能力（需 `.env` 的 `NODE_HOME`）。
- **`main.py`**：统一入口，一条命令跑完顺序确定 → 资源迁移 → Unit 翻译 → 打包 → 构建与反思修复：

```powershell
conda activate A2H
python main.py
```

- `ResourceMigrator` 不再生成简化版 `module.json5` / `build-profile.json5`（模板的完整配置在打包时保留）。
- 已用 `test1\Calculator` 验证：翻译 + 打包 + `hvigorw assembleHap` 构建成功。要在模拟器运行，用 DevEco Studio 打开输出工程，配置自动签名（File → Project Structure → Signing Configs）后点 Run；`.hap` 在签名配置后才会产出。

## 后续可做（本次未包含）

- 把 `values` 资源（string/color/dimen）依赖接入文件图，与 `resource_migrator` 的映射打通；
- Unit 粒度的自动评估指标（内聚度 = 组内边 / 跨组边），对 LLM 划分结果打分并自动重试；
- 根据真实项目继续补充 ArkTS 构建错误知识与回归用例。

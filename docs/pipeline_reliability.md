# 翻译与差分修复流程

当前实现撤销了修复候选工程、候选晋级、通过步数评分和候选预算。编译修复与功能修复面向指定的工作工程，功能补丁先完整提案并批量提交，循环只保留一条主线：

```text
检查当前构建 → 全量差分回放 → 根据最新报告修复相关文件 → 重建 → 失败轨迹诊断复测 → 全量验收
```

编译失败时进入编译修复循环；构建通过后才能部署。所有轨迹完整通过才结束为 `FULL_GATE_PASSED`。部分修复直接保留，下一轮使用新的失败证据，不要求每次修改立刻提升一致率。

## 本次精简解决的问题

- 旧记录 `.a2h_runs/g5` 中，启动修复让 6 条轨迹开始执行动作，但验证通过步数仍为 0，候选机制因此拒绝保留修改。现在允许继续修复已暴露的深层问题。
- 功能修复循环先确认失败能够稳定复现；只有 `cause_class=translation` 且确认复现的报告才会进入自动修复。平台中介、基础设施、基线、预算和未知原因会以类型化状态停止，不会修改业务代码。
- 修复后的局部复测只评价本轮选中的失败轨迹；局部通过后必须执行全量验收，未选轨迹的 `NOT_RUN` 不再被误判为局部复测失败。
- 系统图库等外部表面必须由具体事件声明 `ExternalSurfaceContract`。协议只允许带稳定目标的 `CLICK`，并要求源端动作前后应用语义不变；无声明表面、表面缺失或类型不符均不能通过。只有协议匹配后才执行一次声明的恢复动作，再比较恢复后的 L0-L2 状态。
- 外部表面所有权由稳定的系统根 ID 确认；裸 ID、带命名空间的 ID 和路径后缀均可匹配，根 ID 本身即可作为所有权证据，界面文案只用于候选消歧。仅命中文案或无法确认前台所有权时保留完整页面语义，以 `cause_class=unknown` 和 `INCONCLUSIVE` 停止，不能归因成可自动修复的翻译缺陷。
- 外部表面协议使用中性的 `EXTERNAL_PROTOCOL` 报告。已确认仍位于应用页面但声明表面未出现，或已确认出现另一种系统表面时，才可形成翻译侧失败；恢复动作、稳定等待或返回应用失败属于平台问题；已确认的系统表面没有事件声明属于基线问题。修复资格继续只由确认状态和 `cause_class=translation` 决定。
- seed 前缀刷新会复制已有外部表面协议。动作导致前台包切到系统表面时，录制器必须按显式 contract 执行一次恢复动作、等待稳定并确认前台返回源应用，随后才采集 app-owned `post_state`；任一步失败都会拒绝该 seed 前缀。随机探索没有预声明协议，动作后前台包变化或所有权未知时会在保存该事件前终止，因此不会把系统态写成应用基线。
- 指标分别报告 `R_eq_direct`（直接一致）和 `R_eq_policy`（包含合法中介恢复），同时记录 `mediated_steps` 与 `external_recovery_failures`，避免把自动恢复后的通过表述成直接功能一致。
- 外部协议步骤证据使用整数 `schema_version=1`。旧格式、字段缺失、布尔/浮点版本号和与事件声明不一致的证据都不能计为直接或中介成功；声明协议的 PASS 步必须同时具备接受、执行恢复和恢复成功三项证据。
- 每次 replay、confirmation 和预算中断携带的 partial `TraceResult` 都在进入门禁判定前重新校验汇总契约。PASS 必须逐项包含全部计划动作，启动记录不能抵扣动作计数，目标动作必须成功匹配，且 PASS 不能同时携带失败 verdict 或不稳定标记。FAIL/INCONCLUSIVE 必须以最后一条终止步骤准确复述分歧的 status、kind、phase、cause 和动作执行状态；分歧中的 Android 状态必须来自该阶段对应的基线状态。非法结果统一转为 `INFRA_ERROR + INCONCLUSIVE`；FLAKY 会生成独立且契约自洽的 INCONCLUSIVE 结果，持久化前再次校验。
- 同文件的失败证据合并后调用一次模型，提供相关 Android 源码和实际导入依赖。找不到相关文件就明确停止，不扫描全部页面反复调用模型。多文件修复先收集并校验全部提案，再以 write-ahead journal 一次提交源码、同步副本、两侧 manifest 和本轮新建备份。进程内异常立即恢复 before-image；进程被终止后，下次启动依据 ledger 中的 transaction ID 保留已登记提交，否则回滚整批文件。journal 还记录写集合哈希和 manifest 只读依赖，检测陈旧提案及提交期间的源码漂移。
- 功能修复在 schema v3 `repair_ledger.json` 中绑定规范工程身份，并持久记录严格失败指纹、`source_after_patch`、补丁摘要、结构化模型结果和门禁生命周期。同一 workspace 不能串接另一工程的 open attempt。每个构建候选有独立 `candidate_id`，门禁显式关联 attempt、实际测试源码和 `build_proof_sha256`；相同源码重新构建也会关联新的构建证明。diagnostic PASS 只是 provisional，后续 `full_validation` 才形成权威终态，中断后会从该阶段继续。再次遇到相同 `(source, failure, repair_context)`，包括 A→B→A 循环，会在调用模型前以 `NO_PROGRESS` 停止；repair context 覆盖 manifest、计划、`page_pairs.json`、`unit_page_map.json`、修复策略、系统提示词和模型 ID，因此映射或策略变化后可以重新评估。账本加载与追加会重算身份和上下文摘要，并在 workspace 锁内 reload、校验和原子替换，避免并发丢事件。无补丁区分 `UNMAPPED_REPORT`、`MODEL_NO_PROPOSAL`、`NO_OP_PATCH` 和 `INVALID_PATCH`；损坏账本以 `REPAIR_LEDGER_INVALID` 关闭。
- 有真实产物清单时不读取旧翻译计划缓存。差分准备只检查映射事实，不要求启动入口、导航行为已正确；这些问题应在实际回放中暴露并交给修复循环。
- 删除候选补丁引擎及其评分协议；文件事务只负责写盘一致性，不承担候选选择或通过判定。

## 保留的必要检查

`translation_manifest.json` 记录源单元、目标文件和页面映射。新翻译与打包仍检查翻译完整性和页面注册；已有工程可直接构建。差分测试优先使用清单中的实际页面映射，无清单的旧工程必须提供明确的 `page_pairs.json` 和 `unit_page_map.json`，不根据文件命名猜测。已有映射与清单冲突时保留文件并报错。

`.pipeline_build.json` 记录真实构建结果、输入指纹和 HAP 哈希。构建指纹排除项目根级流程状态和构建缓存，但会覆盖资源树中同名的 `tests`、`oracle.json`、`.tmp` 等合法构建输入。BuildFixLoop 在编译前写入 post-build validation pending 标记，只有日志解析和 ArkUI 静态检查都通过才清除；源码变化、构建失败或进程在后置检查中终止后，旧 HAP 都不能被当作当前成果部署。修复后必先重建，再回放；达到最后一轮时也不能跳过验证而报告成功。

Trace v2 保留初始状态、动作前状态和动作后状态。回放接触目标设备前先静态校验状态链：`initial_state` 的应用语义必须等于首事件 `pre_state`，每个事件的 `post_state` 必须等于下一事件的 `pre_state`；比较覆盖页面、文本、控件、值和列表计数，不受截图与 dump 附件路径影响。链路断裂直接判为 `BASELINE_INVALID` 和 `INCONCLUSIVE`。设备故障、无效基线、超时和无法形成结论的测试不会触发业务代码修复，也不会算作通过。报告分别显示执行步数、直接验证步数和中介验证步数；没有可比较证据时一致率显示 `N/A`。外部表面协议只能来自已有 seed 的显式声明，不能根据界面文案自动创建。

修复仅写工程内的相关 ArkTS 文件，不修改测试种子、预期值或比较规则。保留对空实现、删除事件、截断输出的检查。代码块解析保留首行源码；文件使用原子替换批量提交，首次修改前保存 `.bak` 或 `.funcbak`。

工程门禁会在设备部署前生成 `gate/arkts_index.json`。索引记录 `@Entry/@Component struct`、`build`/方法范围、`.id()`、资源、事件和 `router.pushUrl`，并读取 `main_pages.json`。回放种子中的动作 id 若不在对应页面，或源码路由未注册，会写入 `arkts_static_issues.json` 并以 `STATIC_CONTRACT_INVALID` 停止，不把确定的源码契约错误伪装成设备回放失败。功能修复命中索引到的行为 id 时，会把方法行范围放入模型上下文，并拒绝超出该范围的补丁；无法定位时保留原有文件级修复策略。种子选择器也会先调用同一份 Trace v2 基线校验，缺少 `schema_version`、`initial_state` 或事件 `pre_state` 的旧录制只会被报告为跳过，不会复制成门禁种子。也可以离线运行 `python tools/index_arkts.py <工程> --seeds <种子目录> --page-pairs <page_pairs.json> --check`。

## 工作目录与恢复

翻译执行前会将 Unit 依赖图中的强连通分量合并成一个联合翻译任务，保留全部源码和对组外单元的依赖。相互跳转的页面一起规划，不再互相等待完成；联合任务未完整生成时，下游仍会被阻塞。产物清单用联合任务记录源码归属，Activity 到页面的映射仍依据实际输出的 sources。

单元内的 depends_on 可引用本次计划中的输出，以及已成功的直接依赖 Unit 的实际输出文件。文件名规范化后按依赖重新排序；自依赖、不存在的依赖和输出文件间的循环依赖会明确报错，不直接删除依赖边。失败记录的 details 包含具体 Unit、输出文件和错误，便于区分网络失败、非法计划和下游阻塞。

新翻译运行在独立运行目录中保存 `generated` 初始成果和 `packaged` 工作工程。`--resume PROJECT` 跳过翻译和打包，直接修复该工程；有真实产物清单时不读取旧翻译计划缓存。只有显式传入 `--sync-dir` 才把每次修改同步到指定生成工程中的已有文件，同步内容可能尚未通过构建或差分测试。

构建修复失败或外层中断后保留已经完整提交的代码，工程可能暂时无法编译。构建修复的打包侧源码、同步侧源码和两侧 manifest 使用同一可恢复事务，避免同步写入失败后分裂。重新运行时先恢复未完成事务并检查构建来源，必要时先构建修复，再回放。功能修复只有在补丁和对应 `repair_attempt` 同时持久化后才保留提交；ledger 追加失败或强杀发生在登记前时恢复整批 before-image，登记完成后的残留 journal 则在续跑时清理并保留补丁。

`--resume-run RUN_DIR` 继续同一运行并保留累计预算，核对源码、基线、流程实现及工程指纹；更换流程版本后应新建运行目录，通过 `--resume PROJECT` 继续现有工程。

主要记录：运行目录的 `state.json`、事件日志和 `build_result.json`；门禁目录的 `functional_state.json`、`functional_result.json`、`repair_ledger.json`、`repair_*.json`、`history/` 和 `runs/`。`build_transactions/` 与 `repair_transactions/` 只在提交尚未完成清理时存在。设备回放逐条输出即时进度，续跑不会覆盖已占用的回放轮次目录。`rounds` 明确表示门禁调用数，同时单独记录 `full_gate_calls`、`diagnostic_gate_calls` 和 `repair_iterations`。

## 运行参数与准备

- 使用 `D:\Anaconda\envs\A2H\python.exe`，准备可用的 SDK、Node、Hvigor、hdc 和鸿蒙设备。
- 模型连接参数由本机配置读取；需要可用的模型服务。此文档不保存密钥。
- 准备有效 Trace v2 基线，并确认 Android 原源码及页面映射可读取。
- `--max-gate-rounds 3` 表示最多 3 个外层门禁迭代，包含初始全量回放，因此最多进行 2 次功能修复。局部诊断通过后会在同一迭代追加一次全量验收；结果文件用分项计数明确实际调用次数。
- `--max-fix-rounds 4` 表示每次编译循环最多 4 轮修复，最多执行 5 次构建；同时受 `--max-builds` 全局上限约束。
- 主流程、差分门禁和功能修复循环均没有累计墙钟时间上限；模型调用默认不设累计上限，构建默认上限为 12。可用 `--max-model-calls N` 恢复模型调用上限，`--max-model-calls 0` 表示无限。单次模型请求、构建命令和设备命令仍保留超时，避免单个外部调用永久挂死。

下面的命令用于后续实机验收，本次代码修改没有执行它。显式指定新门禁目录，避免复用旧环境配置中的候选运行目录。

```powershell
& 'D:\Anaconda\envs\A2H\python.exe' main.py `
  --resume '.a2h_runs\weather_legacy_repair_20260918\project' `
  --enable-diff-gate `
  --seeds-dir '.a2h_runs\weather_baseline_v2_20260918_2036\traces' `
  --harmony-device '127.0.0.1:5555' `
  --max-gate-rounds 3 --max-fix-rounds 4 `
  --run-dir '.a2h_runs\simple1' --gate-workspace '.a2h_runs\simple1\gate'

# 查看或继续同一运行
& 'D:\Anaconda\envs\A2H\python.exe' main.py --status '.a2h_runs\simple1'
& 'D:\Anaconda\envs\A2H\python.exe' main.py --resume-run '.a2h_runs\simple1'

# 离线回归；临时目录须使用未占用的新路径
& 'D:\Anaconda\envs\A2H\python.exe' -m pytest -q --basetemp '.a2h_runs\pytest_simple_final'
```

## 验证边界

本次使用替身模型、假设备、合成工程和故障注入验证循环顺序、末轮重建复测、异常中断与恢复、构建来源、失败结果契约、外部协议证据、并发账本、源码版本绑定、context-aware 无进展检测、多文件崩溃恢复和同步工程一致性。A2H 环境离线全量回归共 473 项通过；离线通过不代表任何具体翻译应用已通过真实设备差分测试。

修复器目前修改已有 `.ets` 文件。缺少完整页面、需要新增模块或修改非 ArkTS 工程配置时，可能停在无修改或无法定位状态；这类场景需单独提供明确证据后扩展，不能通过放宽比较阈值或改写测试来宣称通过。

撤回前流程代码备份：`.a2h_runs/before_simplify_20260919_014754.zip`。历史运行报告保留供追溯，不作为新流程的候选状态继续使用。

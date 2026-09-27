# A2H 靶子对比实验数据记录规范（给执行实验的 AI）

本文件把《转换成功指标对比报告》第 7 节的主结果表落成可执行的记录要求。报告中的论文数字只作为背景证据；执行实验时只记录实际运行得到的事实，不填猜测值。

## 1. 记录原则

1. 先记录原始事实，再计算指标。每个比例必须同时保存分子、分母和排除原因。
2. 同一批应用、场景、轨迹、设备和环境要供所有可重跑方法复用。
3. 大文件只在 JSON 中记录路径：截图、控件树 dump、构建日志和设备日志不内联。
4. 不能适用的指标写 `N/A`，并在 `na_reason` 说明原因；空白单元格禁止进入最终表。
5. 不把“动作能执行”写成“功能正确”：`Step Replay`、`Complete Script`、`R_eq_direct` 和 `Scenario Pass` 分开记录。

已有的 `Trace`、`StateVector`、`DivergenceReport`、`GateResult`、`RepairReport` 结构继续使用。本规范只补充实验批次、构建部署、成本和汇总字段，不另造第二套轨迹格式。

## 2. 文件层级

每个方法 × 应用 × 版本保存一份 `run_manifest.json`；每条轨迹保存现有的轨迹结果和附件；每轮修复保存 `history/round_NNN.json`；批次结束生成 `report.json` 和 `report.md`。

建议目录：

```text
results/<experiment_id>/<method>/<app_id>/
  run_manifest.json
  traces/<trace_id>.json
  results/<trace_id>/step<N>/       # 截图、dump、日志
  history/round_001.json
  report.json
  report.md
```

## 3. `run_manifest.json` 必记字段

### 3.1 实验身份

| 字段 | 内容 |
|---|---|
| `experiment_id` | 批次唯一 ID |
| `method` | `B0`、`compile_only`、`screenshot_feedback`、`replay_only`、`diff_no_repair`、`diff_repair`、`human_upper` 等 |
| `method_version` | 翻译器/修复器代码提交号或版本 |
| `app_id`、`app_version` | 源应用身份和版本 |
| `source_revision`、`target_revision` | 源项目和目标项目提交号；外部论文靶子填 `paper_reported` |
| `scenario_set_id` | 使用的场景集版本 |
| `trace_set_hash` | 轨迹集内容哈希 |
| `started_at`、`finished_at` | 墙钟时间边界 |

### 3.2 环境和输入控制

记录以下值，缺失任何一项都不能声称“公平比较”：

```json
{
  "environment": {
    "android_device": "",
    "harmony_device": "",
    "android_os": "",
    "harmony_os": "",
    "sdk_versions": {"android": "", "harmony": ""},
    "screen_size": "",
    "density": "",
    "orientation": "",
    "locale": "",
    "timezone": "",
    "network_mode": "offline|mock|live",
    "data_reset": true,
    "page_pairs_version": "",
    "oracle_config_version": "",
    "mask_config_version": "",
    "whitelist_version": ""
  }
}
```

同时保存 APK/HAP 哈希、安装包路径、构建命令、构建工具版本和网络 mock 配置路径。

### 3.3 构建与部署

```json
{
  "build": {
    "attempts": 0,
    "success": false,
    "initial_errors": 0,
    "remaining_errors": 0,
    "log_path": ""
  },
  "deploy": {
    "attempts": 0,
    "install_success": false,
    "launch_success": false,
    "stable_start_success": false,
    "log_path": ""
  }
}
```

判定规则：构建命令成功且无未忽略编译错误才算 `build.success`；安装、启动并通过稳定性检查才算 `deploy.stable_start_success`。失败输入保留在分母中。

## 4. 轨迹和逐步记录

沿用现有 `Trace` 和 `StateVector`，每个事件至少保留：

- `trace_id`、`scenario_id`、`step`、`action`、目标控件指纹和参数；
- 安卓端 `pre_state`、`post_state` 及其哈希；鸿蒙端 `actual_state`；
- `match_kind`、`match_score`、`executed`、`wait_stable`、`step_elapsed_ms`；
- L0、L1、L2 判定及失败谓词；
- 截图、dump、日志路径；
- `visual_metric_name`、`visual_score`、遮罩版本（视觉指标只作辅助，不替代 L0-L2）。

对每条轨迹保存这些汇总字段：

| 字段 | 记录内容 |
|---|---|
| `total_steps` | 轨迹原计划步数 |
| `executed_steps` | 实际执行到的步数 |
| `matched_steps` | 成功找到目标控件的步数 |
| `replayed_steps` | 找到控件且动作执行完成的步数 |
| `complete_script` | 是否执行到最后一步 |
| `scenario_pass` | 所有步骤、断言和终态检查是否通过 |
| `first_divergence_step` | 首个真实语义分叉步；无分叉为 `null` |
| `divergence_kind` | `EXEC_UNMAPPED`、`EXEC_AMBIGUOUS`、`L0_CRASH`、`L1_PAGE`、`L2_CONTENT`、`TIMEOUT` 等 |
| `category` | `TRANSLATION`、`PLATFORM`、`SOURCE_BUG`、`NOISE`、`UNCONFIRMED` |
| `confirmed_runs` | 同输入复跑次数与复现次数 |
| `artifacts_dir` | 证据目录 |

分叉后默认停止该轨迹，避免把一个首分叉扩散成多个失败；如启用中介恢复，必须记录插入、删除或替代的步骤数。

## 5. 指标计算所需的原始计数

在 `report.json` 中同时保存以下计数和比例：

| 结果表列 | 原始计数（必须保存） | 计算口径 |
|---|---|---|
| `Build %` | `build_pass`、`all_input_projects` | `build_pass / all_input_projects` |
| `Deploy %` | `deploy_pass`、`all_input_projects` | 安装、启动、稳定首屏均通过 |
| `Visual` | 每个比较检查点的分数、指标名、遮罩、样本数 | 明确 SSIM（高好）或 LPIPS（低好） |
| `Step Replay %` | `replayed_steps`、`total_steps` | 只表示动作可落地并执行 |
| `Complete Script %` | `complete_scripts`、`all_scripts` | 一条脚本的所有动作执行结束 |
| `R_eq_direct` | 未恢复且通过 L0-L2 的步骤、有明确结论的已执行步骤 | 不把未执行步骤静默删除 |
| `Scenario Pass` | 全部步骤和终态通过的场景、有效场景总数 | 一个关键步骤失败即失败 |
| `First-div F1` | 系统首分叉、人工标注首分叉的 TP/FP/FN | 只在有人工 ground truth 的评测子集计算 |
| `Repair %` | 修复后通过且全量回归无退化的缺陷、进入修复的已确认缺陷 | 无修复流程为 `N/A` |
| `Regression %` | 修复后新失败的原通过轨迹、修复前原通过轨迹 | 越低越好 |

`E2E-FP` 作为总指标另外保存：同时构建、部署、回放、功能断言和回归通过的应用/场景数 ÷ 全部输入应用/场景数。

应用级的 Build/Deploy 与场景级的 Replay/状态指标不能混用分母。汇总时同时保留 `micro` 原始计数和按应用平均的 `macro` 值。

## 6. 首分叉和人工复核

每个候选分叉至少记录：

```json
{
  "first_divergence_step": 3,
  "system_prediction": {"kind": "L2_CONTENT", "unit": "Unit_Profile"},
  "manual_ground_truth": {"step": 3, "unit": "Unit_Profile", "label": "TRANSLATION"},
  "replay_confirmation": {"runs": 3, "same_step": 3, "same_kind": 3},
  "suspect_units": ["Unit_Profile"],
  "artifacts": {
    "android_png": "",
    "harmony_png": "",
    "android_dump": "",
    "harmony_dump": "",
    "log_tail": ""
  }
}
```

平台合法差异、源应用自身异常、设备故障和不稳定复现必须有独立标签，不能混入翻译缺陷。

## 7. 修复轮次和成本

每轮 `history/round_NNN.json` 记录：

- 本轮输入 HAP/代码哈希、改动的翻译单元、运行轨迹和跳过轨迹；
- 本轮构建次数、部署结果、通过/失败/FLAKY/超时轨迹；
- 每个修复报告的首分叉、疑似单元、修复 diff 路径、修复前后结果；
- 全量回归集的原通过轨迹数、新失败轨迹数；
- 本轮耗时。

批次级成本至少拆成：

```json
{
  "cost": {
    "wall_clock_s": 0,
    "generation_s": 0,
    "build_s": 0,
    "deploy_s": 0,
    "replay_s": 0,
    "repair_s": 0,
    "regression_s": 0,
    "repair_rounds": 0,
    "first_pass_round": null,
    "llm_calls": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "money": null,
    "human_minutes": 0
  }
}
```

`money` 不可可靠取得时写 `N/A` 并说明计费方式；不要用调用次数冒充费用。

## 8. 最小自检清单

- [ ] 所有方法使用同一 `scenario_set_id` 和 `trace_set_hash`；
- [ ] 每个比例都有分子、分母和失败/排除原因；
- [ ] 构建或部署失败的输入没有从 E2E 分母删除；
- [ ] 每个分叉有首分叉步、类别、复跑证据和附件路径；
- [ ] 修复组做了全量回归，并记录新增失败；
- [ ] 外部论文数字标记为 `paper_reported`，没有伪造缺失字段；
- [ ] `report.json` 可由轨迹明细重新计算，`report.md` 只是展示层。

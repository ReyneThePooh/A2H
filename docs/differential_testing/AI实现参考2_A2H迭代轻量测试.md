# AI 实现参考 · 二：A2H 迭代循环内的轻量差分测试（环内门禁版）

> 本文档面向 AI 编码代理，作为把差分测试嵌入 A2H 翻译迭代流程的规格说明。
> 前置阅读：《差分测试设计文档.md》第六章（原理）、《AI实现参考1_完整差分测试链路.md》（复用其模块）。
> 定位：轻量版**不是新系统**，是完整版模块的子集编排 + 与 A2H 流水线的集成契约。

---

## 1. 角色定位与设计约束

轻量版在 A2H 迭代循环中充当**功能质量门禁（quality gate）**：

```
A2H 每轮迭代:  Unit 翻译/修复 → 编译构建 HAP → 部署 → [轻量差分回放] → 通过?
                     ▲                                        │
                     └────────── 分叉报告（修复输入） ←────────┘ 未通过
```

硬性约束：
- **单轮总耗时 ≤ 5 分钟**（与一次 hvigor 构建同量级）；
- **信号确定性**：同一 HAP 两次运行结论必须一致（因此环内不做随机 fuzz，只回放固定种子轨迹）；
- **输出面向修复**：失败时必须给出可定位到翻译单元（Unit）的结构化报告，而不是笼统的"测试失败"。

复用完整版（diff_tester 包）的模块：`adapters/`、`normalize.py`、`matcher.py`、`oracle.py`（只用 L0–L2）、`state.py`、`replayer.py`、`schemas.py`。**不引入**：`explorer.py`（fuzz 探索）、`attribution.py`（四分类归因）、L3/L4 预言。

## 2. 新增代码的目录结构

```
diff_tester/
  gate/
    __init__.py
    seed_recorder.py    # 半自动种子录制（安卓端，人操作、程序记录）
    selector.py         # 增量轨迹选择（Unit → 页面 → 轨迹）
    gate_runner.py      # 单轮门禁执行器（主入口）
    repair_report.py    # 分叉报告 → 修复环节输入格式
    flaky.py            # FLAKY（不稳定）观察名单管理
```

## 3. 与 A2H 流水线的集成契约

### 3.1 门禁的调用接口（A2H 侧调用）

```python
from diff_tester.gate import run_gate

result = run_gate(GateRequest(
    hap_path="out/entry-default.hap",
    changed_units=["Unit_Profile", "Unit_Login"],   # 本轮迭代改动的翻译单元名
    workspace=".diff_gate/",                        # 门禁工作目录（种子/映射/历史）
    harmony_device="127.0.0.1:5555",
    full_replay=False,                              # True=忽略增量、全量回放（出厂检查用）
))
# result: GateResult
```

```python
@dataclass
class GateResult:
    passed: bool
    ran_traces: list[str]           # 本轮实际回放的轨迹 id
    skipped_traces: list[str]       # 增量策略跳过的轨迹 id
    reports: list[RepairReport]     # 失败时非空，见 §6
    flaky: list[str]                # 本轮标记为 FLAKY 的轨迹
    elapsed_s: float
```

### 3.2 依赖 A2H 侧提供的两份数据（放在 workspace 下）

1. **`unit_page_map.json`** — Unit → 页面集合。A2H 翻译计划（`.pipeline_cache/translation_plan.json`）中已有 Unit 与源文件的绑定，由 Activity/布局文件名推导页面名：

```json
{
  "Unit_Profile": {
    "android_pages": ["ProfileActivity", "EditNameActivity"],
    "harmony_pages": ["ProfileAbility/ProfilePage", "ProfileAbility/EditNamePage"]
  },
  "__global__": { "android_pages": ["*"], "harmony_pages": ["*"] }
}
```

约定：改动的 Unit 若涉及全局文件（Application 类、公共 utils、公共组件），A2H 侧应传 `changed_units=["__global__"]`，门禁自动退化为全量回放。映射缺失的 Unit 同样按全量处理（保守优先，反正种子总量 ≤ 10 条）。

2. **`page_pairs.json`** — 安卓页面 ↔ 鸿蒙页面对应表。若翻译器能直接产出（它知道每个 Activity 译成了哪个 Ability/Page），优先用翻译器产出；否则用完整版的页面指纹自动匹配 + 人工确认生成一次。

## 4. 种子轨迹的录制与存储（seed_recorder.py）

**半自动录制模式**（一次性人工参与，安卓端）：

```
python -m diff_tester.gate.record-seeds --apk app.apk --device <serial> --out .diff_gate/seeds/
```

实现方式：轮询对比法。启动后循环 `dump + screenshot`（间隔 300ms），检测到控件树变化时，用"上一帧树 + 本帧树 + 触摸事件坐标（`adb shell getevent` 监听）"反推被操作的控件与动作类型，自动提炼为 AbstractEvent 并记录 post_state 基线。操作者只需在手机上正常演示流程；每条轨迹结束按回车分段并命名。

> 简化兜底：若 getevent 反推实现成本高，M1 阶段允许"交互式录制"——终端列出当前可交互控件编号，操作者敲编号代替真机点击，程序代为执行并记录。效果等价，实现只需 explorer 的执行通道。

**种子规范**：
- 每应用 5–10 条、每条 5–15 个事件；文件名 `seed_NN_业务描述.json`（Trace schema 同完整版）；
- 必须覆盖：应用入口/主导航 ≥1 条；每个核心功能 ≥1 条；跨页面数据流 ≥1 条（A 页写入 → B 页读出）；
- 录制完成后自动执行一次**安卓端自回放校验**（同设备重放，逐步 L0–L2 对比录制基线）：校验不过的种子标记 `INVALID` 拒绝入库——保证种子本身在源平台上是确定性的。

**维护规则**：安卓原应用不变则种子永不重录；`page_pairs.json` 或掩码规则变更时只需重算基线（重放安卓端刷新 post_state），不需人工重演。

## 5. 增量选择与单轮执行（selector.py + gate_runner.py）

```
run_gate(req):
  1. 加载 seeds、unit_page_map、page_pairs
  2. 选择: pages = ∪ unit_page_map[u].android_pages for u in changed_units
     selected = [t for t in seeds if t途经页面 ∩ pages ≠ ∅]      # 轨迹途经页面 = 各事件 post_state.page 去重
     if "__global__" in changed_units or req.full_replay: selected = 全部
  3. 部署: hdc install -r hap; HarmonyAdapter.ensure_ready()
  4. for t in selected:
       harmony.reset_app()
       r = replayer.replay(t, harmony, page_pairs)     # 复用完整版，oracle 只启 L0–L2
       if r.diverged:
           r2 = 立即复跑一次
           if r2 同步骤同 kind 复现 → reports.append(build_repair_report(r))
           else → flaky.mark(t)                         # 本轮放行；连续2轮 FLAKY → 视为失败
  5. passed = (reports 为空)
  6. 落盘 .diff_gate/history/round_{n}.json（含指标趋势：每轮一致率、通过轨迹数）
```

**预算控制**：单条轨迹超时上限 = 事件数 × 8s + 30s 启动余量；全轮硬上限 5 分钟，超时视为失败并在报告注明 `TIMEOUT`（通常意味着鸿蒙端卡死/白屏，本身就是缺陷信号）。

## 6. 分叉报告 → 修复输入（repair_report.py）

```python
@dataclass
class RepairReport:
    trace_id: str
    trace_intent: str            # 种子文件名里的业务描述，给 LLM 的自然语言上下文
    diverged_step: int
    failure_type: str            # ALIGN_FAIL | CRASH | WRONG_PAGE | CONTENT_LOSS | TIMEOUT
    abstract_event: dict         # 分叉步的抽象事件（含目标控件指纹）
    expected: dict               # 来自安卓基线: {page, must_have_texts, values, list_counts}
    actual: dict                 # 鸿蒙实况: {page, missing_texts, extra_texts, crash_sig}
    suspect_units: list[str]     # 由分叉步 expected.page 反查 unit_page_map 得到
    prior_steps_summary: list[str]  # 前序各步一行摘要（"step1 CLICK 登录 → LoginPage ✓"）
    artifacts: dict              # android_png / harmony_png / harmony_dump / hilog_tail 路径
    def to_prompt(self) -> str   # 渲染为给修复 LLM 的 Markdown 段落（中文，含期望/实际对照表）
```

`to_prompt()` 输出模板（修复环节直接拼进 prompt）：

```markdown
## 功能一致性验证失败
业务场景: {trace_intent}；在第 {diverged_step} 步（{action} “{target.text}”）发生分叉。
失败类型: {failure_type}
- 期望（安卓原应用）: 页面 {expected.page}，应出现文本 {must_have_texts}
- 实际（鸿蒙翻译产物）: 页面 {actual.page}，缺失文本 {missing_texts}；崩溃签名 {crash_sig}
疑似问题单元: {suspect_units}
前序步骤(均已通过): {prior_steps_summary}
鸿蒙端当前控件树摘要: {harmony_dump 前若干行}
请修复上述翻译单元中与该交互相关的事件处理/状态更新/页面跳转逻辑。
```

## 7. 迭代协议（A2H 编排侧遵循）

- 每个 Unit 的"翻译 → 构建 → 门禁"循环最多 **3 轮**：3 轮内 `passed=True` → Unit 前进；3 轮后仍失败 → 该 Unit 标记 `NEEDS_HUMAN`，挂起其分叉报告，全局迭代继续（不阻塞其他 Unit）；
- 同一 `RepairReport` 连续两轮完全相同（同步骤同类型）→ 说明修复无效，第 3 轮的修复 prompt 中必须附上"上一轮修复 diff + 仍然失败"的上下文；
- 全部 Unit 完成后执行一次 `full_replay=True` 的**出厂全量回放**，兜住跨 Unit 耦合缺陷；
- 每轮把 `GateResult` 摘要写入历史，供绘制"迭代轮次 × 通过轨迹数"的收敛曲线（论文图）。

## 8. 里程碑

- **G1**：交互式种子录制 + 安卓自回放校验跑通（依赖完整版 M1–M3 完成）；
- **G2**：`run_gate` 端到端：手工注入缺陷的 HAP 上，正确输出 RepairReport 且 `suspect_units` 定位准确；
- **G3**：与 A2H 流水线对接（unit_page_map 自动从 translation_plan.json 生成；门禁作为流水线步骤调用）；
- **G4**：FLAKY 管理、历史趋势、出厂全量回放；
- **G5（可选）**：getevent 全自动录制、多模拟器并行。

## 9. 验收标准

- 确定性：同一 HAP 连续运行 3 次门禁，结论与报告内容一致；
- 时效：10 条种子全量回放 ≤ 5 分钟（单模拟器）；
- 定位质量：注入 5 类缺陷（删按钮回调 / 改错跳转目标 / 丢文案绑定 / 抛异常 / 死循环卡死），门禁全部报出，且 failure_type 与 suspect_units 正确率 ≥ 4/5；
- 报告可用性：RepairReport.to_prompt() 的输出无需人工加工即可作为修复 LLM 的输入段落；
- 隔离性：门禁代码不 import A2H 流水线内部模块（只消费 workspace 下的两份 JSON 契约文件），保证可独立测试。

# AI 实现参考 · 一：完整差分测试链路（离线评估版）

> 本文档面向 AI 编码代理，作为实现"安卓→鸿蒙翻译差分模糊测试"完整链路的规格说明。
> 原理与术语解释见同目录《差分测试设计文档.md》；本文只写实现所需的契约、算法与验收标准。
> 实现语言：Python 3.10+。目标形态：可独立运行的 CLI 工具包（不依赖 A2H 翻译流水线代码）。

---

## 1. 目标与范围

**输入**：
- 一个安卓应用 APK + 对应的鸿蒙翻译产物 HAP；
- 双端设备/模拟器（adb 与 hdc 均可连通）；
- 可选：种子轨迹 JSON、页面对映射表、控件 ID 映射表（翻译器若产出可利用）。

**输出**：
- 抽象事件轨迹库（安卓端录制产物，JSON 文件集）；
- 每条轨迹的回放结果与分叉报告（JSON + 截图 + dump 附件）；
- 汇总指标报告（JSON + Markdown 两份）。

**不在范围内**：修复翻译产物；与 A2H 流水线的集成（见《AI实现参考2》）。

## 2. 技术栈与依赖

```
python >= 3.10
uiautomator2        # 安卓控件操作、dump、截图
adbutils            # adb 封装（uiautomator2 自带依赖）
hmdriver2           # 鸿蒙控件操作（备选：直接 subprocess 调 hdc shell uitest）
Pillow, imagehash   # 截图裁剪与感知哈希
rapidfuzz           # 字符串相似度（编辑距离）
networkx            # UTG 界面跳转图（可选）
pytest              # 单元测试
```

外部命令行工具：`adb`（Android SDK platform-tools）、`hdc`（HarmonyOS SDK toolchains），实现时通过 `shutil.which` 检查并给出可读报错。

网络 mock（M6 之前可不做）：mitmproxy 录制回放模式，或要求被测应用离线可用。

## 3. 目录结构

```
diff_tester/
  __init__.py
  cli.py                 # 入口：record / replay / evaluate 三个子命令
  config.py              # 全局配置 dataclass（阈值、超时、路径）
  schemas.py             # 所有数据结构定义（dataclass + JSON 序列化）
  adapters/
    base.py              # DeviceAdapter 抽象基类
    android.py           # AndroidAdapter
    harmony.py           # HarmonyAdapter
  normalize.py           # 两端 dump → 统一控件树 UNode
  explorer.py            # 源端控件感知探索器（含录制）
  matcher.py             # 控件对齐器
  oracle.py              # 分层预言 L0–L4
  state.py               # 状态抽象函数 alpha、文本掩码、页面指纹
  replayer.py            # 目标端逐步回放 + 分叉管理
  attribution.py         # 复跑确认 + 四分类归因
  metrics.py             # 指标计算
  report.py              # Markdown/JSON 报告生成
tests/                   # 单元测试（normalize/matcher/oracle/metrics 必测）
```

## 4. 核心数据 Schema（schemas.py，全部可 JSON 序列化）

### 4.1 归一化控件节点 UNode

```python
@dataclass
class UNode:
    role: str            # 归一化角色: button|text|textfield|checkbox|switch|list|listitem|image|container|other
    id: str | None       # 资源ID尾段（安卓去掉 "pkg:id/" 前缀；鸿蒙取 id 属性）
    text: str            # 可见文本，无则 ""
    desc: str            # content-desc / accessibility 描述
    abs_bounds: tuple[int, int, int, int]   # (x1, y1, x2, y2) 像素
    rel_bounds: tuple[float, float, float, float]  # 除以屏幕宽高后的比例
    clickable: bool
    editable: bool
    checked: bool | None
    children: list["UNode"]
    # 派生方法: iter_interactive(), tree_hash(), find_all(pred)
```

角色映射表（normalize.py 内置，可配置扩充）：

| 安卓 class 含 | 鸿蒙 type 含 | role |
|---|---|---|
| Button, ImageButton | Button | button |
| EditText | TextInput, TextArea, SearchField | textfield |
| TextView（不可点击） | Text | text |
| CheckBox | Checkbox | checkbox |
| Switch | Toggle, Switch | switch |
| RecyclerView, ListView | List, Grid, Scroll | list |
| ImageView | Image | image |
| 其余 | 其余 | container/other |

### 4.2 抽象事件 AbstractEvent

```python
@dataclass
class TargetFingerprint:
    role: str
    id_hint: str | None
    text: str
    desc: str
    rel_bounds: tuple[float, float, float, float]
    patch_path: str | None      # 录制时目标控件区域截图（裁剪自整屏图）

@dataclass
class AbstractEvent:
    step: int
    action: str                 # CLICK|LONG_CLICK|TYPE|SWIPE|BACK|HOME|ROTATE|WAIT_IDLE
    target: TargetFingerprint | None   # BACK/HOME/SWIPE(全屏)/WAIT_IDLE 时为 None
    params: dict                # TYPE: {"text": ...}; SWIPE: {"direction": "up", "dist": 0.5}
    pre_state_hash: str         # 执行前安卓端 alpha 向量哈希
    post_state: "StateVector"   # 执行后安卓端状态基线（完整保存，回放时作期望值）
```

### 4.3 状态向量 StateVector（alpha 的输出）

```python
@dataclass
class StateVector:
    page: str                   # Activity 类名 / Ability+路由名
    texts: dict[str, int]       # 掩码后可见文本 multiset（文本→出现次数）
    widgets: dict[str, list[str]]  # role → 该role所有控件的文本标签列表
    values: dict[str, str]      # 输入框id/序号 → 当前内容；开关 → checked
    list_counts: dict[str, int] # 列表控件 → 可见条目数
    alive: bool
    crash_sig: str | None       # 命中的崩溃签名行
    screenshot: str             # 截图文件路径（附件，不参与比较）
    dump_path: str              # 原始 dump 文件路径（附件）

    def hash(self) -> str       # page+texts+values 的 sha256，用于 pre_state 校验与稳定判据
```

### 4.4 轨迹 Trace 与分叉报告 DivergenceReport

```python
@dataclass
class Trace:
    trace_id: str
    app_pkg_android: str
    app_pkg_harmony: str
    events: list[AbstractEvent]
    meta: dict                  # 录制时间、设备、种子来源等

@dataclass
class DivergenceReport:
    trace_id: str
    diverged_step: int          # 首分叉步号，从1开始
    kind: str                   # EXEC_UNMAPPED | EXEC_AMBIGUOUS | L0_CRASH | L1_PAGE | L2_CONTENT
    detail: dict                # 各 kind 专属字段：期望/实际 page、缺失文本、匹配得分表等
    android_state: StateVector  # 分叉步的安卓基线
    harmony_state: StateVector | None
    artifacts_dir: str          # 两端截图、dump、日志尾部的存放目录
    confirmed: bool | None      # 归因阶段填写：复跑是否复现
    category: str | None        # 归因阶段填写：TRANSLATION|PLATFORM|SOURCE_BUG|NOISE
```

## 5. 适配器契约（adapters/base.py）

```python
class DeviceAdapter(ABC):
    def ensure_ready(self) -> None            # 设备连通、被测应用已安装，否则抛异常
    def reset_app(self) -> None               # 清应用数据 + 冷启动 + 等待首屏稳定
    def dump_tree(self) -> UNode              # dump 并归一化
    def screenshot(self, path: str) -> None
    def current_page(self) -> str             # Activity / Ability 名
    def execute(self, ev: AbstractEvent, node: UNode | None) -> None  # 原生执行
    def poll_crash(self) -> str | None        # 增量读日志，返回命中的崩溃签名
    def wait_stable(self, timeout_s: float = 10) -> bool
        # 实现：循环 dump_tree().tree_hash()，连续两次相同且间隔>=0.5s 返回 True；超时 False
```

**AndroidAdapter 实现要点**：uiautomator2 `d.dump_hierarchy()` 取 XML；`d.app_current()["activity"]` 取页面；崩溃签名正则 `FATAL EXCEPTION|ANR in`（`adb logcat -d -s AndroidRuntime ActivityManager`，维护读取偏移做增量）；`reset_app` 用 `pm clear` + `am start`。

**HarmonyAdapter 实现要点**：优先 hmdriver2（`Driver.dump_hierarchy()` / 控件对象点击）；hdc 兜底路径全部通过 `subprocess.run(["hdc", ...])` 并设超时；页面名用 hmdriver2 当前 Ability 接口或 `hdc shell aa dump -a` 解析；崩溃签名 `cppcrash|jscrash|appfreeze`（faultlogger 目录或 `hdc shell hilog -x` 过滤）；`reset_app` 用 `bm clean -n <bundle> -d` + `aa start`。**注意 hdc/uitest 在不同鸿蒙版本命令有差异，所有调用点集中封装、失败时打印原始 stderr。**

## 6. 关键算法

### 6.1 状态抽象 alpha（state.py）

```
alpha(adapter) -> StateVector:
  tree = adapter.dump_tree(); page = adapter.current_page()
  texts = multiset( mask(t) for t in tree 所有非空 text/desc )
  mask 规则（内置正则，可配置追加）:
    - 时间/日期: \d{1,2}:\d{2}(:\d{2})?、\d{4}[-/年]\d{1,2}[-/月]\d{1,2}
    - 纯数字长串(>=6位)、百分比进度、电量样式
    - 命中 → 替换为占位符 "<VOLATILE>"
  widgets/values/list_counts 按 UNode 遍历提取
  crash = adapter.poll_crash()
```

页面指纹（用于自动建页面对映射表）：`fingerprint(page) = (标题区文本, 各role控件数向量)`；首次全量遍历两端后，按指纹余弦相似度做二分图最大匹配，人工确认一遍后落盘 `page_pairs.json`。

### 6.2 控件对齐（matcher.py）

```
match(fp: TargetFingerprint, tree: UNode, action: str) -> MatchResult
  候选 C = tree.iter_interactive() 按动作过滤:
    CLICK/LONG_CLICK → clickable; TYPE → editable
  对每个 c ∈ C:
    sim_id   = ratio(fp.id_hint, c.id)          # rapidfuzz，None 时记 0 且权重转移
    sim_text = ratio(fp.text or fp.desc, c.text or c.desc)
    sim_role = 1.0 if fp.role == c.role else (0.5 if 兼容角色 else 0.0)
    sim_pos  = 1 - min(1, 中心点欧氏距离(rel) / 0.5) 再乘面积比惩罚
    sim_img  = 1 - phash_distance(fp.patch, crop(screenshot, c.abs_bounds)) / 64   # 无patch记0且权重转移
    score = 0.35*sim_id + 0.25*sim_text + 0.10*sim_role + 0.15*sim_pos + 0.15*sim_img
    （某分量缺失时其权重按比例摊给其余分量）
  排序取 top1, top2:
    top1.score >= 0.75 and (top1.score - top2.score) >= 0.10 → MATCHED(top1)
    0.55 <= top1.score < 0.75 或差距不足 → AMBIGUOUS(top1, top2)   # M5 后可接 LLM 仲裁
    否则 → UNMAPPED
```

权重与阈值全部放 `config.py`，实验时可调。**必须写单元测试**：构造合成 UNode 树覆盖 id 保留/id 丢失/纯图标/双胞胎控件四种情形。

### 6.3 分层预言（oracle.py）

```
compare(expected: StateVector, actual: StateVector, page_pairs) -> OracleVerdict
  L0: actual.alive and actual.crash_sig is None      失败 → L0_CRASH
  L1: page_pairs.corresponds(expected.page, actual.page)   失败 → L1_PAGE
  L2: 三项全过，否则 → L2_CONTENT:
      jaccard(expected.texts, actual.texts) >= 0.9        # multiset Jaccard
      widget_align_rate >= 0.8                            # expected.widgets 中控件在 actual 找到匹配的比例（复用 matcher，只用 text+role+pos）
      values/list_counts 逐 key 相等（key 经对齐后比较）
  L3(仅记录): ssim(masked_screenshot_A, masked_screenshot_H) → 存入 detail，不判失败
```

### 6.4 源端探索录制（explorer.py）

```
explore(android: AndroidAdapter, n_traces, max_steps, seed_prefix=None):
  for k in range(n_traces):
    android.reset_app(); trace = Trace(...)
    if seed_prefix: 逐事件按 matcher 在安卓自身树上定位并执行（自回放校验种子有效）
    for step in range(max_steps):
      tree = android.dump_tree()
      cand = 可交互控件列表; 若空 → BACK 或终止
      选择策略: 权重 = 2.0(该控件指纹从未操作过) | 1.0(已操作过)
                10% 概率改选系统事件 BACK; TYPE 时从语料库抽文本
                （语料库: 合法邮箱/手机号/中英混排/超长200字符/emoji/空串/含'--的串）
      fp = 提炼指纹(node, screenshot)      # 先截图裁 patch，再执行
      android.execute(ev, node); android.wait_stable()
      ev.post_state = alpha(android); trace.events.append(ev)
    落盘 traces/{trace_id}.json + artifacts/
```

### 6.5 目标端回放（replayer.py）

```
replay(trace, harmony: HarmonyAdapter, page_pairs) -> TraceResult
  harmony.reset_app()
  for ev in trace.events:
    tree = harmony.dump_tree()
    if ev.target:
        m = match(ev.target, tree, ev.action)
        if m.kind != MATCHED: return 分叉(EXEC_UNMAPPED/AMBIGUOUS, ev.step, m.detail)
    harmony.execute(ev, m.node); ok = harmony.wait_stable()
    actual = alpha(harmony)
    verdict = compare(ev.post_state, actual, page_pairs)
    if not verdict.passed: return 分叉(verdict.kind, ev.step, verdict.detail)
  return 通过
```

分叉时收集 artifacts：两端截图、鸿蒙 dump、日志尾 200 行，存 `results/{trace_id}/step{i}/`。

### 6.6 归因（attribution.py）

对每份 DivergenceReport：同轨迹在鸿蒙端复跑 3 次 → 复现次数 <3 → NOISE；全复现 → 依次查平台白名单正则（`whitelist.yaml`：权限弹窗文本、系统 UI 组件名等）→ PLATFORM；安卓端复跑该轨迹亦异常 → SOURCE_BUG；否则 TRANSLATION，并按 kind 映射缺陷四型（崩溃/导航/内容/状态）。

### 6.7 指标（metrics.py）

按设计文档 §4.9 公式实现：R_replay、R_eq、轨迹通过率、归一化首分叉深度、页面覆盖对齐率、控件召回率（UNMAPPED 计数）、缺陷谱。输出 `report.json` 与人读的 `report.md`（含每条轨迹一行的明细表 + 汇总）。

## 7. CLI 设计（cli.py）

```
python -m diff_tester record  --apk app.apk --device <adb_serial> \
       --traces 20 --max-steps 30 --seeds seeds/ --out traces/
python -m diff_tester replay  --hap app.hap --device <hdc_serial> \
       --traces traces/ --page-pairs page_pairs.json --out results/
python -m diff_tester evaluate --results results/ --confirm-runs 3 --out report/
```

三个子命令可独立运行（录制一次、回放多次）。所有配置项支持 `--config config.yaml` 覆盖默认值。

## 8. 里程碑（按序实现，每步可独立验收）

- **M1 适配器与归一化**：双端 `dump_tree/screenshot/current_page/reset_app` 跑通；单测：两端真实 dump 样本 → UNode 树字段正确。
- **M2 状态抽象与预言**：alpha + compare 纯函数完成；单测：构造等价/不等价状态对，验证 L0–L2 判定。
- **M3 对齐器**：match 完成；单测覆盖四种情形；在 1 个真实应用对上人工标注 30 个控件对，报告 top1 准确率（验收 ≥ 80%，不足则调权重）。
- **M4 录制与回放**：手写 3 条种子在 demo 应用上录制→回放全通；人为注入一个翻译缺陷（改掉 HAP 某按钮回调）验证能报出正确的首分叉步与 kind。
- **M5 归因与指标**：evaluate 子命令出完整报告。
- **M6 探索器**：控件感知随机探索替代手写种子，单应用 20 条轨迹无框架级异常。
- **M7（可选）**：AMBIGUOUS 的 LLM 仲裁、L3 SSIM、mitmproxy 网络 mock、多模拟器并行。

## 9. 验收标准与注意事项

- 全链路在"1 个安卓 demo 应用 + 其鸿蒙翻译产物"上端到端跑通，报告可复现（同输入两次运行指标一致）；
- 任何 hdc/adb 子进程调用必须设超时与重试（1 次），失败信息包含原始命令与 stderr；
- 截图/dump 等大文件只存路径入 JSON，不内联；
- 阈值（0.75/0.55/0.9/0.8）、权重、掩码正则、白名单全部外置可配，代码内不硬编码；
- 日志分级：每步一行 INFO（step/action/match_score/verdict），分叉时 WARNING 带 artifacts 路径。

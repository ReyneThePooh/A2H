"""核心数据结构定义（全部可 JSON 序列化）。

对应《AI实现参考1》§4。大文件（截图、dump）只存路径入 JSON，不内联。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional

Bounds = tuple[int, int, int, int]
RelBounds = tuple[float, float, float, float]

#: 抽象动作词表
ACTIONS = ("CLICK", "LONG_CLICK", "TYPE", "SWIPE", "BACK", "HOME", "ROTATE", "WAIT_IDLE")

#: 归一化角色词表
ROLES = ("button", "text", "textfield", "checkbox", "switch",
         "list", "listitem", "image", "container", "other")


# ---------------------------------------------------------------------------
# 4.1 归一化控件节点
# ---------------------------------------------------------------------------

@dataclass
class UNode:
    """两端 dump 归一化后的统一控件树节点。"""

    role: str
    id: Optional[str]
    text: str
    desc: str
    abs_bounds: Bounds                  # (x1, y1, x2, y2) 像素
    rel_bounds: RelBounds               # 除以屏幕宽高后的比例
    clickable: bool
    editable: bool
    checked: Optional[bool]
    children: list["UNode"] = field(default_factory=list)

    # -- 遍历 ---------------------------------------------------------------

    def iter_all(self) -> Iterator["UNode"]:
        yield self
        for c in self.children:
            yield from c.iter_all()

    def iter_interactive(self) -> Iterator["UNode"]:
        """所有可交互节点（可点击或可编辑）。"""
        for n in self.iter_all():
            if n.clickable or n.editable:
                yield n

    def find_all(self, pred: Callable[["UNode"], bool]) -> list["UNode"]:
        return [n for n in self.iter_all() if pred(n)]

    # -- 派生 ---------------------------------------------------------------

    def tree_hash(self) -> str:
        """整树结构哈希，用于界面稳定判据（§4.6）。"""
        parts = []
        for n in self.iter_all():
            parts.append(f"{n.role}|{n.id}|{n.text}|{n.desc}|{int(n.clickable)}|{n.checked}")
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()

    def center_abs(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.abs_bounds
        return (x1 + x2) // 2, (y1 + y2) // 2

    def center_rel(self) -> tuple[float, float]:
        x1, y1, x2, y2 = self.rel_bounds
        return (x1 + x2) / 2.0, (y1 + y2) / 2.0

    def rel_area(self) -> float:
        x1, y1, x2, y2 = self.rel_bounds
        return max(0.0, x2 - x1) * max(0.0, y2 - y1)

    # -- 序列化 -------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "id": self.id,
            "text": self.text,
            "desc": self.desc,
            "abs_bounds": list(self.abs_bounds),
            "rel_bounds": list(self.rel_bounds),
            "clickable": self.clickable,
            "editable": self.editable,
            "checked": self.checked,
            "children": [c.to_dict() for c in self.children],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "UNode":
        return cls(
            role=d["role"],
            id=d.get("id"),
            text=d.get("text", ""),
            desc=d.get("desc", ""),
            abs_bounds=tuple(d["abs_bounds"]),
            rel_bounds=tuple(d["rel_bounds"]),
            clickable=bool(d.get("clickable", False)),
            editable=bool(d.get("editable", False)),
            checked=d.get("checked"),
            children=[cls.from_dict(c) for c in d.get("children", [])],
        )


# ---------------------------------------------------------------------------
# 4.2 抽象事件
# ---------------------------------------------------------------------------

@dataclass
class TargetFingerprint:
    """目标控件的多重指纹（录制时提炼，回放时用于对齐）。"""

    role: str
    id_hint: Optional[str]
    text: str
    desc: str
    rel_bounds: RelBounds
    patch_path: Optional[str] = None    # 录制时目标控件区域截图（裁剪自整屏图）

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "id_hint": self.id_hint,
            "text": self.text,
            "desc": self.desc,
            "rel_bounds": list(self.rel_bounds),
            "patch_path": self.patch_path,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TargetFingerprint":
        return cls(
            role=d["role"],
            id_hint=d.get("id_hint"),
            text=d.get("text", ""),
            desc=d.get("desc", ""),
            rel_bounds=tuple(d["rel_bounds"]),
            patch_path=d.get("patch_path"),
        )


# ---------------------------------------------------------------------------
# 4.3 状态向量（alpha 的输出）
# ---------------------------------------------------------------------------

@dataclass
class StateVector:
    """状态抽象函数 alpha 的输出（§2.4 / §6.1）。"""

    page: str                            # Activity 类名 / Ability+路由名
    texts: dict[str, int]                # 掩码后可见文本 multiset（文本→出现次数）
    widgets: dict[str, list[str]]        # role → 该 role 所有控件的文本标签列表
    values: dict[str, str]               # 输入框 id/序号 → 当前内容；开关 → checked
    list_counts: dict[str, int]          # 列表控件 → 可见条目数
    alive: bool = True
    crash_sig: Optional[str] = None      # 命中的崩溃签名行
    screenshot: str = ""                 # 截图文件路径（附件，不参与比较）
    dump_path: str = ""                  # 原始 dump 文件路径（附件）

    def hash(self) -> str:
        """page+texts+values 的 sha256，用于 pre_state 校验与稳定判据。"""
        payload = json.dumps(
            {
                "page": self.page,
                "texts": sorted(self.texts.items()),
                "values": sorted(self.values.items()),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        return {
            "page": self.page,
            "texts": dict(self.texts),
            "widgets": {k: list(v) for k, v in self.widgets.items()},
            "values": dict(self.values),
            "list_counts": dict(self.list_counts),
            "alive": self.alive,
            "crash_sig": self.crash_sig,
            "screenshot": self.screenshot,
            "dump_path": self.dump_path,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "StateVector":
        return cls(
            page=d.get("page", ""),
            texts=dict(d.get("texts", {})),
            widgets={k: list(v) for k, v in d.get("widgets", {}).items()},
            values=dict(d.get("values", {})),
            list_counts=dict(d.get("list_counts", {})),
            alive=bool(d.get("alive", True)),
            crash_sig=d.get("crash_sig"),
            screenshot=d.get("screenshot", ""),
            dump_path=d.get("dump_path", ""),
        )


@dataclass
class AbstractEvent:
    """平台无关抽象事件（§2.1 / §4.2）。"""

    step: int
    action: str                                   # 见 ACTIONS
    target: Optional[TargetFingerprint]           # BACK/HOME/全屏SWIPE/WAIT_IDLE 时为 None
    params: dict = field(default_factory=dict)    # TYPE: {"text":...}; SWIPE: {"direction","dist"}
    pre_state_hash: str = ""                      # 执行前安卓端 alpha 向量哈希
    post_state: Optional[StateVector] = None      # 执行后安卓端状态基线（回放时作期望值）

    def to_dict(self) -> dict:
        return {
            "step": self.step,
            "action": self.action,
            "target": self.target.to_dict() if self.target else None,
            "params": dict(self.params),
            "pre_state_hash": self.pre_state_hash,
            "post_state": self.post_state.to_dict() if self.post_state else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AbstractEvent":
        return cls(
            step=int(d["step"]),
            action=d["action"],
            target=TargetFingerprint.from_dict(d["target"]) if d.get("target") else None,
            params=dict(d.get("params", {})),
            pre_state_hash=d.get("pre_state_hash", ""),
            post_state=StateVector.from_dict(d["post_state"]) if d.get("post_state") else None,
        )


# ---------------------------------------------------------------------------
# 4.4 轨迹与分叉报告
# ---------------------------------------------------------------------------

@dataclass
class Trace:
    trace_id: str
    app_pkg_android: str
    app_pkg_harmony: str
    events: list[AbstractEvent] = field(default_factory=list)
    meta: dict = field(default_factory=dict)      # 录制时间、设备、种子来源等

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "app_pkg_android": self.app_pkg_android,
            "app_pkg_harmony": self.app_pkg_harmony,
            "events": [e.to_dict() for e in self.events],
            "meta": dict(self.meta),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Trace":
        return cls(
            trace_id=d["trace_id"],
            app_pkg_android=d.get("app_pkg_android", ""),
            app_pkg_harmony=d.get("app_pkg_harmony", ""),
            events=[AbstractEvent.from_dict(e) for e in d.get("events", [])],
            meta=dict(d.get("meta", {})),
        )


#: 分叉 kind 取值
DIVERGENCE_KINDS = ("EXEC_UNMAPPED", "EXEC_AMBIGUOUS", "L0_CRASH", "L1_PAGE", "L2_CONTENT")

#: 归因 category 取值
CATEGORIES = ("TRANSLATION", "PLATFORM", "SOURCE_BUG", "NOISE")

#: kind → 缺陷四型（崩溃/导航/内容/状态）
KIND_TO_DEFECT_TYPE = {
    "L0_CRASH": "crash",
    "L1_PAGE": "navigation",
    "L2_CONTENT": "content",
    "EXEC_UNMAPPED": "state",
    "EXEC_AMBIGUOUS": "state",
}


@dataclass
class DivergenceReport:
    trace_id: str
    diverged_step: int                   # 首分叉步号，从 1 开始
    kind: str                            # 见 DIVERGENCE_KINDS
    detail: dict                         # 各 kind 专属字段
    android_state: Optional[StateVector]  # 分叉步的安卓基线
    harmony_state: Optional[StateVector]
    artifacts_dir: str = ""
    confirmed: Optional[bool] = None     # 归因阶段填写：复跑是否复现
    category: Optional[str] = None       # 归因阶段填写：见 CATEGORIES

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "diverged_step": self.diverged_step,
            "kind": self.kind,
            "detail": self.detail,
            "android_state": self.android_state.to_dict() if self.android_state else None,
            "harmony_state": self.harmony_state.to_dict() if self.harmony_state else None,
            "artifacts_dir": self.artifacts_dir,
            "confirmed": self.confirmed,
            "category": self.category,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DivergenceReport":
        return cls(
            trace_id=d["trace_id"],
            diverged_step=int(d["diverged_step"]),
            kind=d["kind"],
            detail=dict(d.get("detail", {})),
            android_state=StateVector.from_dict(d["android_state"]) if d.get("android_state") else None,
            harmony_state=StateVector.from_dict(d["harmony_state"]) if d.get("harmony_state") else None,
            artifacts_dir=d.get("artifacts_dir", ""),
            confirmed=d.get("confirmed"),
            category=d.get("category"),
        )


# ---------------------------------------------------------------------------
# 对齐结果 / 预言判定 / 回放结果
# ---------------------------------------------------------------------------

@dataclass
class MatchResult:
    """控件对齐结果（§6.2）。"""

    kind: str                            # MATCHED | AMBIGUOUS | UNMAPPED
    node: Optional[UNode]
    score: float
    second_score: float = 0.0
    detail: dict = field(default_factory=dict)   # top 候选得分表


@dataclass
class OracleVerdict:
    """分层预言比较结果（§6.3）。"""

    passed: bool
    kind: Optional[str] = None           # L0_CRASH | L1_PAGE | L2_CONTENT
    detail: dict = field(default_factory=dict)


@dataclass
class StepRecord:
    """回放过程中每一步的执行记录（供指标计算）。"""

    step: int
    action: str
    expected_page: str = ""
    match_kind: Optional[str] = None     # 无 target 的事件为 None
    match_score: Optional[float] = None
    verdict_kind: Optional[str] = None   # None 表示比较通过
    passed: bool = True
    unstable: bool = False

    def to_dict(self) -> dict:
        return {
            "step": self.step, "action": self.action,
            "expected_page": self.expected_page,
            "match_kind": self.match_kind, "match_score": self.match_score,
            "verdict_kind": self.verdict_kind, "passed": self.passed,
            "unstable": self.unstable,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "StepRecord":
        return cls(
            step=int(d["step"]), action=d["action"],
            expected_page=d.get("expected_page", ""),
            match_kind=d.get("match_kind"), match_score=d.get("match_score"),
            verdict_kind=d.get("verdict_kind"), passed=bool(d.get("passed", True)),
            unstable=bool(d.get("unstable", False)),
        )


@dataclass
class TraceResult:
    """一条轨迹的回放结果。"""

    trace_id: str
    total_steps: int
    executed_steps: int
    passed: bool
    first_divergence_step: Optional[int] = None
    divergence: Optional[DivergenceReport] = None
    steps: list[StepRecord] = field(default_factory=list)
    android_pages: list[str] = field(default_factory=list)
    harmony_pages: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "total_steps": self.total_steps,
            "executed_steps": self.executed_steps,
            "passed": self.passed,
            "first_divergence_step": self.first_divergence_step,
            "divergence": self.divergence.to_dict() if self.divergence else None,
            "steps": [s.to_dict() for s in self.steps],
            "android_pages": list(self.android_pages),
            "harmony_pages": list(self.harmony_pages),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TraceResult":
        return cls(
            trace_id=d["trace_id"],
            total_steps=int(d["total_steps"]),
            executed_steps=int(d["executed_steps"]),
            passed=bool(d["passed"]),
            first_divergence_step=d.get("first_divergence_step"),
            divergence=DivergenceReport.from_dict(d["divergence"]) if d.get("divergence") else None,
            steps=[StepRecord.from_dict(s) for s in d.get("steps", [])],
            android_pages=list(d.get("android_pages", [])),
            harmony_pages=list(d.get("harmony_pages", [])),
        )


def save_json(obj: Any, path: str) -> None:
    """统一的 JSON 落盘（UTF-8、缩进、不转义中文）。"""
    import os
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

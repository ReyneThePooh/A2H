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

# External surfaces are accepted only through an explicit, closed protocol.
# Keeping these vocabularies beside the serialized schema lets recorders,
# validators, and replayers share one contract without importing device code.
EXTERNAL_SURFACES = frozenset({"photo_picker"})
EXTERNAL_SURFACE_POLICIES = frozenset({"dismiss_and_compare"})
EXTERNAL_SURFACE_RECOVERY_ACTIONS = frozenset({"BACK"})
EXTERNAL_SURFACE_OWNERSHIPS = frozenset({"system"})
EXTERNAL_PROTOCOL_EVIDENCE_SCHEMA_VERSION = 1

#: 归一化角色词表
ROLES = ("button", "text", "textfield", "checkbox", "switch",
         "list", "listitem", "image", "container", "other")

# Generated overlays expose this stable ID so both platform dumps describe
# the currently active interaction surface instead of different window trees.
ACTIVE_SCOPE_ID = "a2h_active_scope"


def external_protocol_evidence_errors(evidence: Any) -> list[str]:
    """Validate machine evidence emitted for one external-surface decision."""
    if not isinstance(evidence, dict):
        return ["external protocol evidence must be an object"]

    required = {
        "id", "schema_version", "declared_surface", "actual_surface",
        "ownership", "policy", "accepted", "recovery_attempted",
        "recovered", "recovery_action",
    }
    errors = [f"external protocol evidence missing {key}"
              for key in sorted(required - set(evidence))]
    if errors:
        return errors
    if evidence["id"] != "external_protocol":
        errors.append("external protocol evidence has an unknown id")
    if (type(evidence["schema_version"]) is not int
            or evidence["schema_version"] != EXTERNAL_PROTOCOL_EVIDENCE_SCHEMA_VERSION):
        errors.append("external protocol evidence schema version is unsupported")
    for key in ("accepted", "recovery_attempted", "recovered"):
        if type(evidence[key]) is not bool:
            errors.append(f"external protocol evidence {key} must be boolean")

    declared = evidence["declared_surface"]
    actual = evidence["actual_surface"]
    ownership = evidence["ownership"]
    policy = evidence["policy"]
    recovery_action = evidence["recovery_action"]
    if declared is not None and declared not in EXTERNAL_SURFACES:
        errors.append("external protocol evidence declares an unsupported surface")
    if actual is not None and (not isinstance(actual, str) or not actual):
        errors.append("external protocol evidence actual_surface must be a non-empty string or null")
    if ownership not in EXTERNAL_SURFACE_OWNERSHIPS | {"unknown"}:
        errors.append("external protocol evidence ownership is unsupported")
    if policy is not None and policy not in EXTERNAL_SURFACE_POLICIES:
        errors.append("external protocol evidence policy is unsupported")
    if recovery_action is not None and recovery_action not in EXTERNAL_SURFACE_RECOVERY_ACTIONS:
        errors.append("external protocol evidence recovery action is unsupported")

    accepted = evidence["accepted"] is True
    attempted = evidence["recovery_attempted"] is True
    recovered = evidence["recovered"] is True
    if accepted and (declared is None or actual != declared or ownership != "system"):
        errors.append("accepted external protocol evidence does not match its declaration")
    if accepted and (policy not in EXTERNAL_SURFACE_POLICIES
                     or recovery_action not in EXTERNAL_SURFACE_RECOVERY_ACTIONS):
        errors.append("accepted external protocol evidence has no valid recovery policy")
    if attempted and not accepted:
        errors.append("external recovery was attempted before protocol acceptance")
    if recovered and not (accepted and attempted):
        errors.append("external recovery success lacks accepted attempted evidence")
    return errors


def semantic_widget_role(role: str, clickable: bool) -> str:
    """Return the user-facing role used across matching and state comparison."""
    if role == "image" and clickable:
        return "button"
    return role


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
    external_surface: Optional[str] = None  # 系统所有的中介表面（如系统图库）

    def application_semantics(self) -> dict[str, Any]:
        """Return the stable app-owned state, excluding transport evidence."""
        return {
            "page": self.page,
            "texts": dict(self.texts),
            "widgets": {role: sorted(labels) for role, labels in self.widgets.items()},
            "values": dict(self.values),
            "list_counts": dict(self.list_counts),
        }

    def hash(self) -> str:
        """page+texts+values 的 sha256，用于 pre_state 校验与稳定判据。"""
        state = {
            "page": self.page,
            "texts": sorted(self.texts.items()),
            "values": sorted(self.values.items()),
        }
        # Keep hashes for existing Trace v2 baselines stable. Newly recorded
        # external surfaces must still be distinguishable from their app page.
        if self.external_surface is not None:
            state["external_surface"] = self.external_surface
        payload = json.dumps(
            state,
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
            "external_surface": self.external_surface,
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
            external_surface=d.get("external_surface"),
        )


@dataclass(frozen=True)
class ExternalSurfaceContract:
    """Recorded protocol for a system-owned surface triggered by one event."""

    surface: str
    policy: str = "dismiss_and_compare"
    recovery_action: str = "BACK"
    ownership: str = "system"

    def to_dict(self) -> dict:
        return {
            "surface": self.surface,
            "policy": self.policy,
            "recovery_action": self.recovery_action,
            "ownership": self.ownership,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ExternalSurfaceContract":
        return cls(
            surface=str(data.get("surface", "")),
            policy=str(data.get("policy", "")),
            recovery_action=str(data.get("recovery_action", "")),
            ownership=str(data.get("ownership", "")),
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
    pre_state: Optional[StateVector] = None       # v2: 完整前态，跨平台用语义比较
    external_surface: Optional[ExternalSurfaceContract] = None

    def to_dict(self) -> dict:
        return {
            "step": self.step,
            "action": self.action,
            "target": self.target.to_dict() if self.target else None,
            "params": dict(self.params),
            "pre_state_hash": self.pre_state_hash,
            "post_state": self.post_state.to_dict() if self.post_state else None,
            "pre_state": self.pre_state.to_dict() if self.pre_state else None,
            "external_surface": (self.external_surface.to_dict()
                                 if self.external_surface else None),
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
            pre_state=StateVector.from_dict(d["pre_state"]) if d.get("pre_state") else None,
            external_surface=(ExternalSurfaceContract.from_dict(d["external_surface"])
                              if d.get("external_surface") else None),
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
    schema_version: int = 2
    initial_state: Optional[StateVector] = None

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "app_pkg_android": self.app_pkg_android,
            "app_pkg_harmony": self.app_pkg_harmony,
            "events": [e.to_dict() for e in self.events],
            "meta": dict(self.meta),
            "schema_version": self.schema_version,
            "initial_state": self.initial_state.to_dict() if self.initial_state else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Trace":
        return cls(
            trace_id=d["trace_id"],
            app_pkg_android=d.get("app_pkg_android", ""),
            app_pkg_harmony=d.get("app_pkg_harmony", ""),
            events=[AbstractEvent.from_dict(e) for e in d.get("events", [])],
            meta=dict(d.get("meta", {})),
            schema_version=int(d.get("schema_version", 1)),
            initial_state=StateVector.from_dict(d["initial_state"]) if d.get("initial_state") else None,
        )

    def baseline_errors(self) -> list[str]:
        """Validate evidence before touching a device; legacy hashes are not states."""
        errors = []
        if self.schema_version != 2:
            errors.append("Trace v2 required; re-record initial_state and event pre_state")
        if not self.events:
            errors.append("trace has no events")
        states = [("initial_state", self.initial_state)]
        for index, event in enumerate(self.events, 1):
            if event.step != index:
                errors.append(f"event step must be sequential: expected {index}, got {event.step}")
            if event.action not in ACTIONS:
                errors.append(f"step {event.step}: unsupported action {event.action}")
            states.extend([(f"step {event.step} pre_state", event.pre_state),
                           (f"step {event.step} post_state", event.post_state)])
            if event.pre_state and event.pre_state_hash and event.pre_state.hash() != event.pre_state_hash:
                errors.append(f"step {event.step}: recorded pre_state_hash does not match pre_state")
            contract = event.external_surface
            if contract is None:
                continue

            target_identity = (
                (event.target.id_hint, event.target.text, event.target.desc)
                if event.target is not None else ()
            )
            has_stable_target = any(
                isinstance(value, str) and bool(value.strip())
                for value in target_identity
            )
            if event.action != "CLICK" or not has_stable_target:
                errors.append(
                    f"step {event.step}: external surface contract requires a targeted CLICK"
                )
            if contract.surface not in EXTERNAL_SURFACES:
                errors.append(
                    f"step {event.step}: unsupported external surface {contract.surface!r}"
                )
            if contract.policy not in EXTERNAL_SURFACE_POLICIES:
                errors.append(
                    f"step {event.step}: unsupported external surface policy {contract.policy!r}"
                )
            if contract.recovery_action not in EXTERNAL_SURFACE_RECOVERY_ACTIONS:
                errors.append(
                    f"step {event.step}: unsupported external recovery action "
                    f"{contract.recovery_action!r}"
                )
            if contract.ownership not in EXTERNAL_SURFACE_OWNERSHIPS:
                errors.append(
                    f"step {event.step}: unsupported external surface ownership "
                    f"{contract.ownership!r}"
                )
            if (contract.policy == "dismiss_and_compare"
                    and event.pre_state and event.post_state
                    and (event.pre_state.application_semantics()
                         != event.post_state.application_semantics())):
                errors.append(
                    f"step {event.step}: dismiss_and_compare requires unchanged "
                    "source application semantics"
                )

        if self.events:
            first_pre_state = self.events[0].pre_state
            if (self.initial_state is not None
                    and first_pre_state is not None
                    and (self.initial_state.application_semantics()
                         != first_pre_state.application_semantics())):
                errors.append(
                    "state chain discontinuity: initial_state does not match "
                    f"step {self.events[0].step} pre_state"
                )
            for previous, current in zip(self.events, self.events[1:]):
                if (previous.post_state is not None
                        and current.pre_state is not None
                        and (previous.post_state.application_semantics()
                             != current.pre_state.application_semantics())):
                    errors.append(
                        "state chain discontinuity: "
                        f"step {previous.step} post_state does not match "
                        f"step {current.step} pre_state"
                    )
        for name, state in states:
            if state is None or not state.page:
                errors.append(f"{name} missing or has no page identity")
            elif not state.alive or state.crash_sig:
                errors.append(f"{name} is not a healthy source baseline")
            if state is not None and state.external_surface is not None:
                errors.append(
                    f"{name} must be app-owned; external mediation belongs in the event contract"
                )
        return errors


#: 分叉 kind 取值
DIVERGENCE_KINDS = ("EXEC_UNMAPPED", "EXEC_AMBIGUOUS", "L0_CRASH", "L1_PAGE", "L2_CONTENT",
                    "EXTERNAL_PROTOCOL",
                    "INFRA_ERROR", "BASELINE_INVALID", "STARTUP_STATE_MISMATCH",
                    "PRECONDITION_MISMATCH", "BUDGET_EXHAUSTED", "UNSTABLE")

#: 归因 category 取值
CATEGORIES = ("TRANSLATION", "PLATFORM", "SOURCE_BUG", "NOISE")

#: kind → 缺陷四型（崩溃/导航/内容/状态）
KIND_TO_DEFECT_TYPE = {
    "L0_CRASH": "crash",
    "L1_PAGE": "navigation",
    "L2_CONTENT": "content",
    "EXEC_UNMAPPED": "state",
    "EXEC_AMBIGUOUS": "state",
    "EXTERNAL_PROTOCOL": "state",
}


@dataclass
class DivergenceReport:
    trace_id: str
    diverged_step: int                   # 首分叉步号（0 = 启动阶段崩溃）
    kind: str                            # 见 DIVERGENCE_KINDS
    detail: dict                         # 各 kind 专属字段
    android_state: Optional[StateVector]  # 分叉步的安卓基线
    harmony_state: Optional[StateVector]
    artifacts_dir: str = ""
    confirmed: Optional[bool] = None     # 归因阶段填写：复跑是否复现
    category: Optional[str] = None       # 归因阶段填写：见 CATEGORIES
    phase: str = "compare"
    cause_class: str = "unknown"
    action_executed: bool = False
    # A post-action L2 content mismatch is evidence, but does not stop replay.
    # Keep this explicit so consumers do not infer replay control flow from the
    # (legacy) singular ``divergence`` field.
    soft_failure: bool = False

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
            "phase": self.phase, "cause_class": self.cause_class,
            "action_executed": self.action_executed,
            "soft_failure": self.soft_failure,
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
            phase=d.get("phase", "compare"), cause_class=d.get("cause_class", "unknown"),
            action_executed=bool(d.get("action_executed", False)),
            soft_failure=bool(d.get("soft_failure", False)),
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
    passed: bool = False
    unstable: bool = False
    phase: str = "compare"
    cause_class: str = ""
    action_executed: bool = False
    verified: bool = False
    status: str = "NOT_RUN"
    external_surface: Optional[dict] = None
    soft_failed: bool = False
    failure_detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "step": self.step, "action": self.action,
            "expected_page": self.expected_page,
            "match_kind": self.match_kind, "match_score": self.match_score,
            "verdict_kind": self.verdict_kind, "passed": self.passed,
            "unstable": self.unstable,
            "phase": self.phase, "cause_class": self.cause_class,
            "action_executed": self.action_executed, "verified": self.verified,
            "status": self.status,
            "external_surface": (dict(self.external_surface)
                                 if self.external_surface is not None else None),
            "soft_failed": self.soft_failed,
            "failure_detail": dict(self.failure_detail),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "StepRecord":
        return cls(
            step=int(d["step"]), action=d["action"],
            expected_page=d.get("expected_page", ""),
            match_kind=d.get("match_kind"), match_score=d.get("match_score"),
            verdict_kind=d.get("verdict_kind"), passed=bool(d.get("passed", False)),
            unstable=bool(d.get("unstable", False)),
            phase=d.get("phase", "compare"), cause_class=d.get("cause_class", ""),
            action_executed=bool(d.get("action_executed", False)),
            verified=bool(d.get("verified", False)), status=d.get("status", "NOT_RUN"),
            external_surface=(dict(d["external_surface"])
                              if d.get("external_surface") is not None else None),
            soft_failed=bool(d.get("soft_failed", False)),
            failure_detail=dict(d.get("failure_detail", {})),
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
    status: str = ""
    verified_steps: int = 0
    stop_reason: str = ""
    # New replay evidence. ``divergence`` remains the first report for
    # consumers written against the v1/v2 single-report contract.
    divergences: list[DivergenceReport] = field(default_factory=list)
    soft_failed_steps: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.status:
            self.status = "PASS" if self.passed else "FAIL"
        if not self.divergences and self.divergence is not None:
            self.divergences = [self.divergence]
        elif self.divergence is None and self.divergences:
            self.divergence = self.divergences[0]
        if not self.soft_failed_steps:
            self.soft_failed_steps = [s.step for s in self.steps if s.soft_failed]

    def contract_errors(self, trace: Optional[Trace] = None) -> list[str]:
        """Recompute replay summaries and reject internally inconsistent evidence.

        A replay may now retain several post-action ``L2_CONTENT`` reports and
        continue to later events.  The old singular ``divergence`` field is
        treated as the first report, while any non-soft report remains the one
        that explains why execution stopped.
        """
        errors: list[str] = []
        allowed_statuses = {"PASS", "FAIL", "INCONCLUSIVE", "NOT_RUN"}
        reports = list(self.divergences)
        if not reports and self.divergence is not None:
            reports = [self.divergence]
        if self.divergence is not None and reports and self.divergence is not reports[0]:
            if self.divergence.to_dict() != reports[0].to_dict():
                errors.append("divergence does not match the first divergence report")

        if self.status not in allowed_statuses:
            errors.append(f"unsupported result status: {self.status}")
        if self.total_steps < 0:
            errors.append("total_steps must be non-negative")
        if not 0 <= self.executed_steps <= self.total_steps:
            errors.append("executed_steps is outside the trace bounds")
        if not 0 <= self.verified_steps <= self.total_steps:
            errors.append("verified_steps is outside the trace bounds")
        if self.passed != (self.status == "PASS"):
            errors.append("passed flag disagrees with result status")

        if trace is not None:
            if self.trace_id != trace.trace_id:
                errors.append("result trace_id does not match the replayed trace")
            if self.total_steps != len(trace.events):
                errors.append("result total_steps does not match the replayed trace")

        positive_steps: list[int] = []
        executed_from_steps = 0
        verified_from_steps = 0
        soft_records: list[int] = []
        for index, record in enumerate(self.steps):
            if record.status not in allowed_statuses:
                errors.append(f"step {record.step}: unsupported status {record.status}")
            if record.verified and not record.action_executed:
                errors.append(f"step {record.step}: verified without executing the action")
            if record.passed and not (
                    record.action_executed and record.verified and record.status == "PASS"):
                errors.append(f"step {record.step}: passed without complete PASS evidence")
            if record.passed and record.verdict_kind is not None:
                errors.append(f"step {record.step}: passed with a failure verdict")
            if record.passed and record.unstable:
                errors.append(f"step {record.step}: passed with unstable state evidence")
            if record.passed and record.match_kind not in (None, "MATCHED"):
                errors.append(f"step {record.step}: passed with a failed target match")
            if record.status == "PASS" and not record.passed:
                errors.append(f"step {record.step}: PASS status disagrees with passed flag")
            if record.soft_failed:
                soft_records.append(record.step)
                if not (record.action_executed and record.verified
                        and not record.passed and record.status == "FAIL"):
                    errors.append(
                        f"step {record.step}: soft failure lacks executed comparison evidence"
                    )
                if record.verdict_kind != "L2_CONTENT" or record.phase != "compare":
                    errors.append(
                        f"step {record.step}: only post-action L2_CONTENT may be soft-failed"
                    )
                if record.cause_class != "translation":
                    errors.append(
                        f"step {record.step}: soft failure cause must be translation"
                    )
                if not record.failure_detail:
                    errors.append(f"step {record.step}: soft failure lacks content detail")
            if record.external_surface is not None:
                errors.extend(
                    f"step {record.step}: {error}"
                    for error in external_protocol_evidence_errors(record.external_surface)
                )

            if record.step == 0:
                if index != 0 or record.action != "launch":
                    errors.append("step 0 is reserved for the launch record")
                if (record.action_executed or record.verified or record.passed
                        or record.status == "PASS"):
                    errors.append("launch record cannot count as a replayed action")
            else:
                positive_steps.append(record.step)
                if not 1 <= record.step <= self.total_steps:
                    errors.append(f"step {record.step}: outside the trace bounds")
                elif trace is not None and record.step > len(trace.events):
                    errors.append(f"step {record.step}: outside the replayed trace bounds")
                elif trace is not None:
                    event = trace.events[record.step - 1]
                    if record.action != event.action:
                        errors.append(f"step {record.step}: action does not match the replayed trace")
                    contract = event.external_surface
                    evidence = record.external_surface
                    if record.passed and contract is not None and evidence is None:
                        errors.append(
                            f"step {record.step}: declared external protocol has no evidence"
                        )
                    if event.target is None:
                        if record.match_kind is not None:
                            errors.append(
                                f"step {record.step}: targetless action contains match evidence"
                            )
                    elif (event.target is not None and record.passed
                          and record.match_kind != "MATCHED"):
                        errors.append(
                            f"step {record.step}: targeted PASS lacks a successful match"
                        )
                    if evidence is not None:
                        if contract is None:
                            if record.passed:
                                errors.append(
                                    f"step {record.step}: external protocol success has no declaration"
                                )
                        else:
                            expected_protocol = {
                                "declared_surface": contract.surface,
                                "ownership": contract.ownership,
                                "policy": contract.policy,
                                "recovery_action": contract.recovery_action,
                            }
                            for key, expected in expected_protocol.items():
                                if evidence.get(key) != expected:
                                    errors.append(
                                        f"step {record.step}: external protocol {key} "
                                        "does not match the replayed trace"
                                    )
                            if record.passed and not all(
                                    evidence.get(key) is True
                                    for key in ("accepted", "recovery_attempted", "recovered")):
                                errors.append(
                                    f"step {record.step}: external protocol PASS lacks recovery evidence"
                                )
                executed_from_steps += int(record.action_executed)
                verified_from_steps += int(
                    record.action_executed and record.verified
                    and record.passed and record.status == "PASS"
                )

        if positive_steps and positive_steps != list(range(1, max(positive_steps) + 1)):
            errors.append("step records are not a contiguous replay prefix")
        if self.executed_steps != executed_from_steps:
            errors.append("executed_steps disagrees with step evidence")
        if self.verified_steps != verified_from_steps:
            errors.append("verified_steps disagrees with step evidence")
        if self.soft_failed_steps != sorted(set(soft_records)):
            errors.append("soft_failed_steps disagrees with step evidence")

        if self.status == "PASS":
            if positive_steps != list(range(1, self.total_steps + 1)):
                errors.append("PASS result does not contain every planned step")
            if any(not (record.action_executed and record.verified
                        and record.passed and record.status == "PASS")
                   for record in self.steps if record.step > 0):
                errors.append("PASS result contains a non-passing planned step")
            if self.executed_steps != self.total_steps or self.verified_steps != self.total_steps:
                errors.append("PASS result is not completely executed and verified")
            if reports or self.first_divergence_step is not None:
                errors.append("PASS result contains divergence evidence")
            if self.stop_reason:
                errors.append("PASS result contains a stop reason")
        elif self.status in {"FAIL", "INCONCLUSIVE"} and not reports:
            errors.append(f"{self.status} result has no divergence evidence")

        report_steps: list[int] = []
        terminal_reports: list[DivergenceReport] = []
        for report in reports:
            report_steps.append(report.diverged_step)
            if report.trace_id != self.trace_id:
                errors.append("divergence trace_id does not match the result")
            if not 0 <= report.diverged_step <= self.total_steps:
                errors.append("divergence step is outside the trace bounds")
            if self.status not in {"FAIL", "INCONCLUSIVE"}:
                errors.append("divergence belongs to a non-failing result")
            for key, expected in (
                    ("phase", report.phase),
                    ("cause_class", report.cause_class),
                    ("action_executed", report.action_executed)):
                if key in report.detail and report.detail[key] != expected:
                    errors.append(f"divergence detail {key} disagrees with its typed field")

            record = next((item for item in self.steps
                           if item.step == report.diverged_step), None)
            if record is None:
                errors.append("divergence has no matching step record")
            else:
                if record.verdict_kind != report.kind:
                    errors.append("divergence verdict disagrees with step evidence")
                if record.phase != report.phase:
                    errors.append("divergence phase disagrees with step evidence")
                if record.cause_class != report.cause_class:
                    errors.append("divergence cause disagrees with step evidence")
                if record.action_executed != report.action_executed:
                    errors.append("divergence execution flag disagrees with step evidence")

            is_soft = bool(report.soft_failure or (record and record.soft_failed))
            if is_soft:
                if not (report.kind == "L2_CONTENT" and report.phase == "compare"
                        and report.cause_class == "translation"
                        and report.action_executed):
                    errors.append("only post-action translation L2_CONTENT may be soft-failed")
                if record is None or not record.soft_failed:
                    errors.append("soft divergence is missing its soft-failed step marker")
            else:
                terminal_reports.append(report)

            expected_state = None
            if trace is not None:
                if report.diverged_step == 0:
                    expected_state = trace.initial_state
                elif 1 <= report.diverged_step <= len(trace.events):
                    event = trace.events[report.diverged_step - 1]
                    expected_state = (
                        event.pre_state
                        if report.phase in {"precondition", "match", "execute"}
                        else event.post_state
                    )
            if (expected_state is not None and report.android_state is not None
                    and expected_state.application_semantics()
                    != report.android_state.application_semantics()):
                errors.append("divergence Android state disagrees with the trace baseline")
            if (report.cause_class == "translation"
                    and (expected_state is None or report.android_state is None)):
                errors.append("translation divergence lacks an authoritative Android state")

        # Every soft-failed step is an independently observed divergence.  A
        # result that keeps the step marker but drops its report would otherwise
        # pass this contract (when another divergence is present), causing the
        # gate and confirmation logic to compare an incomplete failure set.
        reported_steps = set(report_steps)
        for step in soft_records:
            if step not in reported_steps:
                errors.append(
                    f"soft-failed step {step} has no matching divergence report"
                )

        if report_steps != sorted(report_steps) or len(report_steps) != len(set(report_steps)):
            errors.append("divergence reports are not ordered and unique by step")
        if reports:
            if self.first_divergence_step != reports[0].diverged_step:
                errors.append("first_divergence_step disagrees with divergence evidence")
            if self.divergence is None:
                errors.append("divergence compatibility field is missing")
            elif self.divergence.to_dict() != reports[0].to_dict():
                errors.append("divergence compatibility field is not the first report")

        if terminal_reports:
            terminal_report = terminal_reports[-1]
            if len(terminal_reports) != 1 or reports[-1] is not terminal_report:
                errors.append("hard divergence must be the final divergence report")
            terminal = self.steps[-1] if self.steps else None
            if terminal is None or terminal.step != terminal_report.diverged_step:
                errors.append("hard divergence has no matching terminal step record")
            elif terminal.soft_failed or terminal.passed:
                # Preserve the established contract error strings because the
                # gate uses them when explaining quarantined evidence.
                if terminal.passed:
                    errors.append("divergence terminal step is marked passed")
                if terminal.soft_failed:
                    errors.append("hard divergence terminal step is marked soft")
            elif (terminal.status != self.status
                  and not (self.status == "INCONCLUSIVE"
                           and self.stop_reason == "FLAKY")):
                errors.append("terminal step status disagrees with result status")
            if (terminal is not None and terminal.step == terminal_report.diverged_step
                    and terminal.verdict_kind != terminal_report.kind):
                errors.append("terminal verdict disagrees with divergence kind")
            if (terminal is not None and terminal.step == terminal_report.diverged_step
                    and terminal.phase != terminal_report.phase):
                errors.append("terminal phase disagrees with divergence phase")
            if (terminal is not None and terminal.step == terminal_report.diverged_step
                    and terminal.cause_class != terminal_report.cause_class):
                errors.append("terminal cause disagrees with divergence cause")
            if (terminal is not None and terminal.step == terminal_report.diverged_step
                    and terminal.action_executed != terminal_report.action_executed):
                errors.append("terminal execution flag disagrees with divergence evidence")
            if self.stop_reason not in {terminal_report.kind, "FLAKY"}:
                errors.append("stop_reason disagrees with hard divergence")
        elif reports and self.status == "FAIL":
            if positive_steps != list(range(1, self.total_steps + 1)):
                errors.append("soft-only FAIL result did not replay every planned step")
            if self.executed_steps != self.total_steps:
                errors.append("soft-only FAIL result did not execute every planned step")
            if self.stop_reason:
                errors.append("soft-only FAIL result contains a hard stop reason")
        elif reports and self.status == "INCONCLUSIVE" and not terminal_reports:
            # The only valid soft-only inconclusive result is the gate's
            # explicit FLAKY downgrade after a complete replay. An incomplete
            # result needs a terminal infrastructure or budget divergence.
            if self.stop_reason != "FLAKY":
                errors.append(
                    "INCONCLUSIVE soft-only result lacks a terminal divergence"
                )
            elif (positive_steps != list(range(1, self.total_steps + 1))
                  or self.executed_steps != self.total_steps):
                errors.append("FLAKY soft-only result did not complete the replay")
        return errors

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "total_steps": self.total_steps,
            "executed_steps": self.executed_steps,
            "passed": self.passed,
            "first_divergence_step": self.first_divergence_step,
            "divergence": self.divergence.to_dict() if self.divergence else None,
            "divergences": [d.to_dict() for d in self.divergences],
            "steps": [s.to_dict() for s in self.steps],
            "android_pages": list(self.android_pages),
            "harmony_pages": list(self.harmony_pages),
            "status": self.status, "verified_steps": self.verified_steps,
            "stop_reason": self.stop_reason,
            "soft_failed_steps": list(self.soft_failed_steps),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TraceResult":
        legacy = "status" not in d or "verified_steps" not in d
        divergence = (DivergenceReport.from_dict(d["divergence"])
                      if d.get("divergence") else None)
        reports = [DivergenceReport.from_dict(item)
                   for item in d.get("divergences", [])]
        if not reports and divergence is not None:
            reports = [divergence]
        return cls(
            trace_id=d["trace_id"],
            total_steps=int(d["total_steps"]),
            executed_steps=int(d["executed_steps"]),
            passed=bool(d["passed"]) and not legacy,
            first_divergence_step=d.get("first_divergence_step"),
            divergence=divergence,
            divergences=reports,
            steps=[StepRecord.from_dict(s) for s in d.get("steps", [])],
            android_pages=list(d.get("android_pages", [])),
            harmony_pages=list(d.get("harmony_pages", [])),
            status="INCONCLUSIVE" if legacy else d["status"],
            verified_steps=int(d.get("verified_steps", 0)),
            stop_reason="LEGACY_RESULT_REQUIRES_REPLAY" if legacy else d.get("stop_reason", ""),
            soft_failed_steps=list(d.get("soft_failed_steps", [])),
        )


def save_json(obj: Any, path: str) -> None:
    """统一的 JSON 落盘（UTF-8、缩进、不转义中文）。"""
    import os
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    import tempfile
    directory = os.path.dirname(os.path.abspath(path))
    fd, temporary = tempfile.mkstemp(prefix=".json-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

"""直接修复当前工程：构建 → 回放 → 修复 → 重建 → 回放。

每轮使用最新报告；部分修复留在工程中继续完善，不按通过率决定是否保留。
只有完整回放通过才报告成功。编译失败、设备故障、取消均保存明确的停止状态。
"""
from __future__ import annotations

import difflib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from hello_agents.core.llm import HelloAgentsLLM
from analyzers.arkts_index import load_arkts_index, validate_patch_scope
from diff_tester.oracle import PagePairs
from pipeline.agents import LLMCallError, create_pipeline_llm
from pipeline.artifacts import (content_hash, load_artifact_manifest,
                                 project_source_fingerprint, refresh_artifact_hashes,
                                 updated_artifact_manifest)
from pipeline.build_fixer import BuildFixLoop
from pipeline.file_transaction import (FileBatchTransaction,
                                       FileTransactionError,
                                       recover_file_transactions)
from pipeline.gate_bridge import load_plan, prepare_workspace, run_diff_gate, unit_ets_files
from pipeline.gate_decision import (REPAIRABLE_TRANSLATION_FAILURES,
                                    TRANSLATION_CAUSE_ALLOWLIST, decide_gate,
                                    is_repairable_report)
from pipeline.repair_ledger import (RepairLedger, RepairLedgerError,
                                    aggregate_failure_identity,
                                    stable_project_identity)
from pipeline.runner import build_is_current
from run_control import (BudgetExceeded, FileLock, atomic_json, atomic_write,
                         check_budget, deadline_scope, file_sha256, fingerprint)


SYSTEM_PROMPT = """你负责修复 Android 到 HarmonyOS 翻译后的行为差异。
依据 Android 源码、实际依赖接口和差分报告修复业务逻辑。不得修改测试、预言、
期望值，不得硬编码测试结果、删除交互或把业务实现替换为空方法。
只返回当前文件的完整 ArkTS 代码，放在 ```typescript 代码块中。
构建与设备回放由外层流程执行，你负责提出修复，不需要预测验证结论。
遵循 ArkTS 限制，禁止 any/unknown 或无关强制类型断言。
若证据不足或本文件无关，说明具体原因，不输出代码。"""

REPAIR_CONTEXT_SCHEMA = 2
REPAIR_POLICY_VERSION = 1


def repair_context_fingerprint(root: Path, plan_path, plan: dict,
                               model_id: str, *,
                               workspace: str | Path | None = None
                               ) -> dict[str, Any]:
    """Fingerprint every input that can change a repair decision."""
    plan_file = Path(plan_path).resolve() if plan_path else None
    policy = {
        "translation_causes": sorted(TRANSLATION_CAUSE_ALLOWLIST),
        "repairable_failures": sorted(REPAIRABLE_TRANSLATION_FAILURES),
    }
    work = Path(workspace).resolve() if workspace is not None else None
    context = {
        "schema_version": REPAIR_CONTEXT_SCHEMA,
        "manifest_sha256": file_sha256(root / "translation_manifest.json"),
        "plan_sha256": (file_sha256(plan_file) if plan_file and plan_file.is_file()
                        else fingerprint(plan)),
        "policy_version": REPAIR_POLICY_VERSION,
        "policy_sha256": fingerprint(policy),
        "system_prompt_sha256": fingerprint(SYSTEM_PROMPT),
        "model_id": model_id,
        "page_pairs_sha256": (
            file_sha256(work / "page_pairs.json") if work else None
        ),
        "unit_page_map_sha256": (
            file_sha256(work / "unit_page_map.json") if work else None
        ),
    }
    context["digest"] = fingerprint(context)
    return context


@dataclass(frozen=True)
class FileRepairOutcome:
    """Typed result for one model proposal, kept behind the legacy file API."""

    code: str
    diff: Optional[str] = None
    detail: Optional[str] = None
    replacement: Optional[bytes] = field(default=None, repr=False, compare=False)
    original: Optional[bytes] = field(default=None, repr=False, compare=False)

    def to_dict(self, path: Path, root: Path) -> dict[str, Any]:
        result: dict[str, Any] = {
            "file": path.relative_to(root).as_posix(),
            "outcome": self.code,
        }
        if self.detail:
            result["detail"] = self.detail
        return result


@dataclass
class RepairOutcome:
    """Structured repair result with tuple-unpacking compatibility."""

    code: str
    diffs: dict = field(default_factory=dict)
    model_calls: int = 0
    files_changed: int = 0
    file_outcomes: list[dict[str, Any]] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return self.code == "PATCH_APPLIED" and self.files_changed > 0

    def __iter__(self):
        yield self.changed
        yield self.diffs

    def to_dict(self) -> dict[str, Any]:
        result = {
            "code": self.code,
            "model_calls": self.model_calls,
            "files_changed": self.files_changed,
            "files": list(self.file_outcomes),
        }
        if self.details:
            result["details"] = dict(self.details)
        return result


def _coerce_repair_outcome(value) -> RepairOutcome:
    """Accept legacy ``(changed, diffs)`` results from callers and tests."""
    if isinstance(value, RepairOutcome):
        return value
    changed, diffs = value
    return RepairOutcome(
        code="PATCH_APPLIED" if changed else "NO_VALID_PATCH",
        diffs=dict(diffs),
        files_changed=1 if changed else 0,
    )


class FunctionalFixLoop:
    def __init__(self, llm: Optional[HelloAgentsLLM] = None,
                 max_gate_rounds: int = 3, build_fix_rounds: int = 2):
        if max_gate_rounds < 1 or build_fix_rounds < 0:
            raise ValueError("回放轮数须 >= 1，编译修复轮数须 >= 0")
        self.llm = llm
        self.max_gate_rounds = max_gate_rounds
        self.build_fix_rounds = build_fix_rounds

    def run(self, project_dir: str | Path, sync_dir: str | Path | None,
            workspace: str | Path, seeds_dir: Optional[str] = None,
            bundle: Optional[str] = None, device: Optional[str] = None,
            hdc_path: Optional[str] = None, plan_path: str | Path | None = None,
            changed_units: Optional[list[str]] = None,
            time_budget_s: Optional[float] = None) -> dict[str, Any]:
        """最多 max_gate_rounds 次全量回放；默认不设墙钟时间上限。

        changed_units 仅保留旧调用兼容；每轮完整回放，避免增量结果冒充全量通过。
        重新调用即从当前工程重建/回放。
        """
        root, work = Path(project_dir).resolve(), Path(workspace).resolve()
        if not root.is_dir():
            raise FileNotFoundError(root)
        sync_root = Path(sync_dir).resolve() if sync_dir else None
        if sync_root == root:
            sync_root = None
        self._transaction_dir = work / "repair_transactions"
        self._transaction_roots = [root] + ([sync_root] if sync_root else [])
        self._pending_repair_transaction = None
        self._repair_workspace = work
        self._active_patch_scopes: dict[Path, list[dict[str, Any]]] = {}
        history, gate_scopes, reports, attempts = [], [], [], 0
        previous_signatures, previous_diffs = set(), {}
        full_trace_ids: set[str] = set()
        diagnostic_trace_ids: list[str] = []
        ledger: Optional[RepairLedger] = None
        active_gate_scope: Optional[str] = None
        active_gate_source: Optional[str] = None
        active_attempt_id: Optional[str] = None
        validation_attempt_id: Optional[str] = None
        resume_full_validation = False
        model_id = str(
            getattr(self.llm, "model", "")
            or os.getenv("LLM_MODEL_ID", "<default>")
        )

        def counters():
            return {
                "rounds": len(history),
                "rounds_semantics": "gate_calls",
                "gate_calls": len(history),
                "full_gate_calls": sum(
                    scope in {"full", "full_validation"}
                    for scope in gate_scopes
                ),
                "diagnostic_gate_calls": gate_scopes.count("diagnostic"),
                "repair_iterations": attempts,
                "repair_attempts": attempts,
            }

        def state(status, **extra):
            atomic_json(work / "functional_state.json", {
                "status": status, "project_dir": str(root),
                **counters(), **extra})

        def finish(reason, **extra):
            result = {"success": reason == "FULL_GATE_PASSED",
                      "status": "PASSED" if reason == "FULL_GATE_PASSED" else reason,
                      "stop_reason": reason, **counters(),
                      "gate_history": history,
                      "needs_human": [r.to_dict() for r in reports], **extra}
            atomic_json(work / "functional_result.json", result)
            state(result["status"], stop_reason=reason)
            return result

        def record_gate(gate, scope, decision, gate_source, attempt_id):
            if project_source_fingerprint(root) != gate_source:
                raise RepairLedgerError(
                    "REPAIR_SOURCE_DRIFT: project changed while the gate was running"
                )
            summary = gate.to_dict()
            history.append(summary)
            gate_scopes.append(scope)
            if ledger is not None and attempt_id is not None:
                ledger.append_gate_result(
                    attempt_id=attempt_id,
                    scope=scope,
                    tested_source=gate_source,
                    build_proof_sha256=file_sha256(
                        root / ".pipeline_build.json"
                    ),
                    gate_summary=summary,
                    decision=(None if decision is None else {
                        "action": decision.action,
                        "reason": decision.reason,
                    }),
                )

        def build():
            state("building")
            result = BuildFixLoop(llm=self.llm, max_fix_rounds=self.build_fix_rounds).run(
                root, sync_dir=sync_root, workspace=work,
                _project_lock_held=True)
            atomic_json(work / "build_result.json", result)
            return result

        def bind_candidate(attempt_id):
            source = project_source_fingerprint(root)
            proof_path = root / ".pipeline_build.json"
            proof_digest = file_sha256(proof_path)
            if proof_path.is_file():
                try:
                    proof = json.loads(proof_path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise RepairLedgerError(
                        "REPAIR_BUILD_PROOF_INVALID: unreadable build proof"
                    ) from exc
                claimed = proof.get("project_input_sha256")
                if proof.get("status") == "success" and claimed != source:
                    raise RepairLedgerError(
                        "REPAIR_BUILD_PROOF_INVALID: build proof does not match candidate source"
                    )
            ledger.append_candidate_ready(
                attempt_id=attempt_id,
                tested_source=source,
                build_proof_sha256=proof_digest,
            )
            return source

        # Prevent two loops from editing the same project, without copying it.
        with FileLock(root.parent / f".{root.name}.repair.lock"):
            try:
                ledger = RepairLedger(
                    work, project_identity=stable_project_identity(root)
                )
            except RepairLedgerError as exc:
                if str(exc).startswith("REPAIR_LEDGER_PROJECT_MISMATCH"):
                    return {
                        "success": False,
                        "status": "REPAIR_LEDGER_INVALID",
                        "stop_reason": "REPAIR_LEDGER_INVALID",
                        **counters(),
                        "gate_history": history,
                        "needs_human": [],
                        "detail": str(exc),
                    }
                return finish("REPAIR_LEDGER_INVALID", detail=str(exc))
            try:
                recover_file_transactions(
                    self._transaction_dir,
                    owner="functional_repair",
                    roots=self._transaction_roots,
                    keep_committed=ledger.has_transaction,
                )
            except (FileTransactionError, RepairLedgerError) as exc:
                return finish("REPAIR_TRANSACTION_INVALID", detail=str(exc))
            state("running")
            try:
                with deadline_scope(time_budget_s):
                    plan = (load_plan(plan_path)
                            if plan_path and not (root / "translation_manifest.json").is_file()
                            else {"units": []})
                    pending_state = ledger.open_attempt_state()
                    pending = (pending_state["attempt"]
                               if pending_state is not None else None)
                    validation_attempt_id = (
                        pending["attempt_id"] if pending is not None else None
                    )
                    resume_full_validation = bool(
                        pending_state and pending_state["provisional"]
                    )
                    if not build_is_current(root):
                        built = build()
                        if not built["success"]:
                            return finish("BUILD_FAILED", build=built)
                    if validation_attempt_id is not None:
                        bind_candidate(validation_attempt_id)
                    for round_no in range(1, self.max_gate_rounds + 1):
                        check_budget()
                        prepare_workspace(work, plan_path, project_dir=root)
                        scope = (
                            "full_validation" if resume_full_validation
                            else "diagnostic" if diagnostic_trace_ids
                            else "full"
                        )
                        state("replaying", round_no=round_no, replay_scope=scope,
                              trace_ids=diagnostic_trace_ids)
                        print(f"\n差分回放 {round_no}/{self.max_gate_rounds}（{scope}）", flush=True)
                        active_gate_scope = scope
                        active_gate_source = project_source_fingerprint(root)
                        active_attempt_id = validation_attempt_id
                        gate = run_diff_gate(
                            root, work, changed_units=[], full_replay=True,
                            seeds_dir=seeds_dir, device=device, hdc_path=hdc_path,
                            bundle=bundle, install=True, time_budget_s=time_budget_s,
                            confirm_failures=True,
                            trace_ids=diagnostic_trace_ids or None)
                        decision = decide_gate(gate, scope)
                        record_gate(
                            gate, scope, decision, active_gate_source,
                            active_attempt_id,
                        )
                        active_gate_scope = None
                        active_gate_source = None
                        active_attempt_id = None
                        resume_full_validation = False
                        reports = gate.reports
                        self._print_gate_summary(gate)
                        if not diagnostic_trace_ids:
                            full_trace_ids = set(gate.ran_traces)
                        if decision.action == "STOP":
                            validation_attempt_id = None
                            return finish(decision.reason)
                        if decision.action == "VALIDATE_FULL":
                            # A focused replay validates the patch hypothesis only.
                            # Reinstall the verified HAP because the device lock is
                            # released between gate calls and another run may replace it.
                            prepare_workspace(work, plan_path, project_dir=root)
                            state("replaying", round_no=round_no,
                                  replay_scope="full_validation", trace_ids=[])
                            print("\n局部复测通过，执行全量验收", flush=True)
                            active_gate_scope = "full_validation"
                            active_gate_source = project_source_fingerprint(root)
                            active_attempt_id = validation_attempt_id
                            gate = run_diff_gate(
                                root, work, changed_units=[], full_replay=True,
                                seeds_dir=seeds_dir, device=device, hdc_path=hdc_path,
                                bundle=bundle, install=True,
                                time_budget_s=time_budget_s,
                                confirm_failures=True, trace_ids=None)
                            decision = decide_gate(gate, "full_validation")
                            record_gate(
                                gate, "full_validation", decision,
                                active_gate_source, active_attempt_id,
                            )
                            active_gate_scope = None
                            active_gate_source = None
                            active_attempt_id = None
                            reports = gate.reports
                            self._print_gate_summary(gate)
                            diagnostic_trace_ids = []
                            full_trace_ids = set(gate.ran_traces)
                            if decision.action == "STOP":
                                validation_attempt_id = None
                                return finish(decision.reason)
                        if decision.action == "PASS":
                            validation_attempt_id = None
                            return finish("FULL_GATE_PASSED")
                        if decision.action != "REPAIR":
                            raise RuntimeError(f"Unexpected gate decision: {decision.action}")
                        validation_attempt_id = None
                        source_before = project_source_fingerprint(root)
                        failure_identity, failure_fingerprints = aggregate_failure_identity(
                            report.confirmation_fingerprint for report in reports
                        )
                        repair_context = repair_context_fingerprint(
                            root, plan_path, plan, model_id, workspace=work
                        )
                        if ledger.has_attempt(
                                source_before, failure_identity,
                                repair_context["digest"]):
                            return finish(
                                "NO_PROGRESS",
                                source_fingerprint=source_before,
                                failure_identity=failure_identity,
                            )
                        if round_no == self.max_gate_rounds:
                            return finish("ROUND_LIMIT")
                        state("repairing")
                        attempts += 1
                        self._pending_repair_transaction = None
                        try:
                            outcome = _coerce_repair_outcome(self._fix_reports(
                                root, sync_root, plan, reports,
                                previous_signatures, previous_diffs,
                            ))
                            source_after_patch = project_source_fingerprint(root)
                            attempt = ledger.append_attempt(
                                source_before=source_before,
                                source_after_patch=source_after_patch,
                                failure_identity=failure_identity,
                                failure_fingerprints=failure_fingerprints,
                                repair_context=repair_context,
                                outcome=outcome.to_dict(),
                                diffs=outcome.diffs,
                            )
                        except BaseException:
                            transaction = self._pending_repair_transaction
                            if transaction is not None:
                                if ledger.has_transaction(transaction.transaction_id):
                                    transaction.finalize()
                                else:
                                    transaction.rollback()
                                self._pending_repair_transaction = None
                            raise
                        transaction = self._pending_repair_transaction
                        if transaction is not None:
                            transaction.finalize()
                            self._pending_repair_transaction = None
                        atomic_json(work / f"repair_{attempt['sequence']:03d}.json", {
                            **attempt,
                            "diffs": [
                                {
                                    "failure": (list(key)
                                                if isinstance(key, tuple) else str(key)),
                                    "diff": value,
                                }
                                for key, value in outcome.diffs.items()
                            ],
                        })
                        if not outcome.changed:
                            return finish(
                                outcome.code,
                                repair_attempt_id=attempt["attempt_id"],
                                repair_outcome=outcome.to_dict(),
                            )
                        previous_signatures = {r.signature() for r in reports}
                        previous_diffs = outcome.diffs
                        validation_attempt_id = attempt["attempt_id"]
                        built = build()
                        if not built["success"]:
                            return finish("BUILD_FAILED", build=built)
                        bind_candidate(validation_attempt_id)
                        failed_ids = {report.trace_id for report in reports}
                        diagnostic_trace_ids = (
                            sorted(failed_ids)
                            if full_trace_ids and failed_ids < full_trace_ids
                            else [])
            except BudgetExceeded as exc:
                partial = getattr(exc, "gate_result", None)
                if partial is not None:
                    try:
                        record_gate(
                            partial, active_gate_scope or "full", None,
                            active_gate_source or project_source_fingerprint(root),
                            active_attempt_id,
                        )
                    except RepairLedgerError as ledger_exc:
                        return finish(
                            "REPAIR_LEDGER_INVALID", detail=str(ledger_exc)
                        )
                    active_gate_scope = None
                    active_gate_source = None
                    active_attempt_id = None
                    reports = partial.reports
                finish("BUDGET_EXHAUSTED", detail=exc.reason)
                raise
            except RepairLedgerError as exc:
                return finish("REPAIR_LEDGER_INVALID", detail=str(exc))
            except KeyboardInterrupt:
                finish("INTERRUPTED")
                raise
            except LLMCallError as exc:
                finish("MODEL_ERROR", error_type=type(exc).__name__)
                raise
            except Exception as exc:
                finish("ERROR", error_type=type(exc).__name__)
                raise

    def _fix_reports(self, project_dir, sync_dir, plan, reports,
                     prev_signatures, prev_diffs):
        """合并同一文件的报告，每轮每文件只调用一次模型；找不到映射就停止。"""
        if (not reports
                or any(not is_repairable_report(report)
                       or report.evidence.get("confirmed") is not True
                       for report in reports)):
            print("修复报告未确认或不属于翻译缺陷，拒绝调用修复模型。", flush=True)
            return RepairOutcome("INELIGIBLE_REPORT")
        root = Path(project_dir).resolve()
        manifest = (load_artifact_manifest(root)
                    if (root / "translation_manifest.json").is_file() else None)
        index = None
        page_pairs = {}
        index_path = getattr(self, "_repair_workspace", None)
        if index_path is not None:
            candidate = Path(index_path) / "arkts_index.json"
            if candidate.is_file():
                try:
                    index = load_arkts_index(candidate)
                except (OSError, ValueError, TypeError):
                    index = None
            pairs_path = Path(index_path) / "page_pairs.json"
            if pairs_path.is_file():
                try:
                    loaded_pairs = json.loads(pairs_path.read_text(encoding="utf-8"))
                    if isinstance(loaded_pairs, dict):
                        if isinstance(loaded_pairs.get("pairs"), list):
                            page_pairs = {
                                str(pair[0]): str(pair[1])
                                for pair in loaded_pairs["pairs"]
                                if isinstance(pair, (list, tuple)) and len(pair) >= 2
                            }
                        else:
                            page_pairs = {
                                str(key): str(value)
                                for key, value in loaded_pairs.items()
                            }
                except (OSError, UnicodeError, ValueError, TypeError):
                    page_pairs = {}
        by_file = {}
        unresolved = []
        for report in reports:
            files = self._valid_repair_files(
                (root / p for p in report.suspect_files), root)
            if report.phase == "startup":
                launcher = manifest.get("launcher") if manifest else None
                if launcher:
                    files += self._valid_repair_files(
                        [root / launcher["output"]], root)
                files += self._valid_repair_files(
                    (root / "entry/src/main/ets/entryability").glob("*.ets"), root)
            elif not files and manifest:
                files = self._valid_repair_files(
                    self._manifest_page_files(report, manifest, root), root)
            if not files:
                files = self._valid_repair_files(
                    unit_ets_files(report.suspect_units, plan, root), root)
            if not files:
                unresolved.append(report.trace_id)
                continue
            for path in sorted(set(files)):
                by_file.setdefault(path, []).append(report)
        if unresolved:
            print("以下失败无法定位文件，拒绝部分修复："
                  + ", ".join(sorted(set(unresolved))), flush=True)
            return RepairOutcome(
                "UNMAPPED_REPORT",
                details={"unmapped_trace_ids": sorted(set(unresolved))},
            )
        changes = {}
        file_outcomes = []
        proposals = []
        model_calls = 0
        files_changed = 0
        self._active_patch_scopes = {}
        for path, related in sorted(by_file.items(), key=lambda item: item[0].as_posix()):
            check_budget()
            unique = {r.root_cause_key or str(r.signature()): r for r in related}
            evidence = "\n\n".join(r.to_prompt() for r in unique.values())
            context = self._source_context(path, root, manifest)
            if index is not None:
                relative = path.relative_to(root).as_posix()
                identifiers = []
                for report in related:
                    target = report.abstract_event.get("target") or {}
                    if target.get("id_hint"):
                        identifiers.append(str(target["id_hint"]))
                route = ""
                for report in related:
                    page = str(report.expected.get("page") or "")
                    mapped = page_pairs.get(page, page)
                    if mapped:
                        route = str(mapped)
                        break
                    pre_state = report.evidence.get("pre_state") or {}
                    page = str(pre_state.get("page") or "")
                    if page:
                        route = str(page_pairs.get(page, page))
                        break
                scopes = index.repair_regions(relative, identifiers, route)
                self._active_patch_scopes[path] = scopes
                context += "\n\n" + index.repair_context(relative, identifiers, route)
            previous = "\n".join(dict.fromkeys(
                prev_diffs[r.signature()] for r in related
                if r.signature() in prev_signatures and r.signature() in prev_diffs))
            if previous:
                context += "\n上轮改动后问题仍存在，请结合最新报告继续修复：\n" + previous
            self._last_file_repair_outcome = None
            model_calls += 1
            diff = self._fix_file(path, evidence, context)
            file_result = self._last_file_repair_outcome
            if not isinstance(file_result, FileRepairOutcome):
                file_result = FileRepairOutcome(
                    "PATCH_APPLIED" if diff else "MODEL_NO_PROPOSAL",
                    diff=diff,
                )
            proposals.append((path, file_result))
            file_outcomes.append(file_result.to_dict(path, root))
            if diff:
                files_changed += 1
                for report in related:
                    signature = report.signature()
                    changes[signature] = changes.get(signature, "") + diff + "\n"
        staged = [(path, result) for path, result in proposals
                  if result.diff and result.replacement is not None]
        if staged:
            transaction_id = self._commit_file_repairs(
                root, sync_dir, manifest, staged
            )
        elif changes and manifest:
            # Compatibility for injected legacy _fix_file implementations that
            # apply their own writes and only return a diff.
            refresh_artifact_hashes(root)
            transaction_id = None
        else:
            transaction_id = None
        if changes:
            code = "PATCH_APPLIED"
        else:
            codes = {item["outcome"] for item in file_outcomes}
            code = next(iter(codes)) if len(codes) == 1 else "NO_VALID_PATCH"
        details = ({"transaction_id": transaction_id}
                   if transaction_id is not None else {})
        return RepairOutcome(
            code=code,
            diffs=changes,
            model_calls=model_calls,
            files_changed=files_changed,
            file_outcomes=file_outcomes,
            details=details,
        )

    def _commit_file_repairs(self, root: Path, sync_dir, manifest,
                             proposals) -> str:
        """Commit a proposal batch and retain its journal until ledger append."""
        originals: dict[Path, bytes] = {}
        replacements: dict[Path, bytes] = {}
        preconditions: dict[Path, str | None] = {}
        for path, result in proposals:
            current = path.read_bytes()
            if result.original is None or current != result.original:
                raise RuntimeError(
                    f"Repair source changed while proposals were collected: {path}"
                )
            originals[path] = current
            replacements[path] = result.replacement
            preconditions[path] = content_hash(current)
        sync_targets: dict[Path, Path] = {}
        sync_replacements: dict[Path, bytes] = {}
        sync_root = Path(sync_dir).resolve() if sync_dir else None
        if sync_root == root:
            sync_root = None
        if sync_root is not None:
            for path, result in proposals:
                target = (sync_root / path.relative_to(root)).resolve()
                if target.is_file() and target.is_relative_to(sync_root):
                    sync_targets[path] = target
                    sync_replacements[target] = result.replacement
                    preconditions[target] = content_hash(target.read_bytes())

        writes: dict[Path, bytes] = dict(replacements)
        writes.update(sync_replacements)
        for path, original in originals.items():
            backup = path.with_suffix(path.suffix + ".funcbak")
            if not backup.exists():
                writes[backup] = original

        manifest_path = root / "translation_manifest.json"
        if manifest:
            manifest_before = manifest_path.read_bytes()
            updated = updated_artifact_manifest(root, replacements)
            preconditions[manifest_path] = content_hash(manifest_before)
            preconditions.update(BuildFixLoop._manifest_preconditions(
                root, updated, replacements
            ))
            writes[manifest_path] = json.dumps(
                updated, ensure_ascii=False, indent=2, sort_keys=True
            ).encode("utf-8")

        if sync_root is not None and sync_replacements:
            sync_manifest_path = sync_root / "translation_manifest.json"
            if sync_manifest_path.is_file():
                sync_manifest_before = sync_manifest_path.read_bytes()
                sync_manifest = updated_artifact_manifest(
                    sync_root, sync_replacements
                )
                preconditions[sync_manifest_path] = content_hash(
                    sync_manifest_before
                )
                preconditions.update(BuildFixLoop._manifest_preconditions(
                    sync_root, sync_manifest, sync_replacements
                ))
                writes[sync_manifest_path] = json.dumps(
                    sync_manifest, ensure_ascii=False, indent=2, sort_keys=True
                ).encode("utf-8")

        roots = getattr(self, "_transaction_roots", None)
        if roots is None:
            roots = [root]
            if sync_root is not None:
                roots.append(sync_root)
        transaction = FileBatchTransaction(
            journal_dir=getattr(self, "_transaction_dir", None),
            owner="functional_repair",
            roots=roots,
            writes=writes,
            preconditions=preconditions,
            writer=atomic_write,
        )
        transaction.commit()
        self._pending_repair_transaction = transaction
        return transaction.transaction_id

    @staticmethod
    def _valid_repair_files(paths, root: Path) -> list[Path]:
        """Keep existing ArkTS sources inside the generated application tree."""
        source_root = (root / "entry/src/main/ets").resolve()
        valid = []
        for candidate in paths:
            path = Path(candidate).resolve()
            if (path.is_relative_to(source_root)
                    and path.is_file() and path.suffix == ".ets"):
                valid.append(path)
        return valid

    @staticmethod
    def _manifest_page_files(report, manifest, root: Path) -> list[Path]:
        """Locate the page(s) that own the failing state transition."""
        pages = {str(report.expected.get("page") or "")}
        if report.action_executed:
            pre_state = report.evidence.get("pre_state") or {}
            pages.add(str(pre_state.get("page") or ""))
        stems = {PagePairs._stem(page) for page in pages if page}
        files = []
        for entry in manifest.get("pages", []):
            if PagePairs._stem(entry.get("android_activity", "")) in stems:
                files.append(root / entry["output"])
        return files

    @staticmethod
    def _source_context(path, root, manifest):
        blocks = ["实际导入依赖：", BuildFixLoop._local_dependency_context(path, root)]
        if manifest and manifest.get("source_root"):
            relative = path.relative_to(root).as_posix()
            sources = {s for out in manifest["outputs"] if out["path"] == relative
                       for s in out["sources"]}
            source_root = Path(manifest["source_root"]).resolve()
            for source in sorted(sources):
                for base in (source_root, source_root / "app/src/main", source_root / "src/main"):
                    original = (base / source).resolve()
                    if original.is_relative_to(source_root) and original.is_file():
                        blocks.append(f"Android 源文件 {source}:\n{original.read_text(encoding='utf-8')}")
                        break
        return "\n\n".join(blocks)

    def _fix_file(self, fp, report_md, repeat_context):
        result = self._fix_file_detailed(fp, report_md, repeat_context)
        self._last_file_repair_outcome = result
        return result.diff

    def _fix_file_detailed(self, fp, report_md, repeat_context):
        print(f"修复文件：{fp.name}", flush=True)
        original = fp.read_bytes()
        source = fp.read_text(encoding="utf-8")
        prompt = (f"{report_md}\n\n{repeat_context}\n\n当前文件 {fp.name}:\n"
                  f"```typescript\n{source}\n```\n请返回修复后的完整文件。")
        response = self._invoke(prompt)
        fixed = BuildFixLoop._extract_code(response)
        if not fixed:
            print("模型未提出代码补丁。", flush=True)
            return FileRepairOutcome("MODEL_NO_PROPOSAL")
        if fixed.rstrip() == source.rstrip():
            print("模型补丁与当前文件相同。", flush=True)
            return FileRepairOutcome("NO_OP_PATCH")
        if reason := BuildFixLoop._validate_fixed(source, fixed):
            print(f"无效修复输出：{reason}", flush=True)
            return FileRepairOutcome("INVALID_PATCH", detail=reason)
        scopes = getattr(self, "_active_patch_scopes", {}).get(fp, [])
        if scopes:
            scope_issues = validate_patch_scope(source, fixed, scopes)
            if scope_issues:
                detail = json.dumps(scope_issues, ensure_ascii=False, sort_keys=True)
                print(f"修复超出静态索引范围：{detail}", flush=True)
                return FileRepairOutcome("INVALID_PATCH", detail=detail)
        diff = "\n".join(difflib.unified_diff(
            source.splitlines(), fixed.splitlines(),
            fromfile=str(fp), tofile=str(fp), lineterm="",
        ))
        return FileRepairOutcome(
            "PATCH_APPLIED", diff=diff, replacement=fixed.encode("utf-8"),
            original=original,
        )

    def _invoke(self, prompt):
        if self.llm is None:
            self.llm = create_pipeline_llm()
        return self.llm.invoke([{"role": "system", "content": SYSTEM_PROMPT},
                                {"role": "user", "content": prompt}]) or ""

    @staticmethod
    def _print_gate_summary(gate):
        metrics = getattr(gate, "metrics", {}) or {}
        direct_rate = metrics.get("R_eq_direct", metrics.get("R_eq"))
        policy_rate = metrics.get("R_eq_policy", metrics.get("R_eq"))
        direct_text = ("N/A（无已比较步骤）" if direct_rate is None
                       else f"{direct_rate:.1%}")
        policy_text = ("N/A（无已比较步骤）" if policy_rate is None
                       else f"{policy_rate:.1%}")
        direct_steps = metrics.get("direct_verified_steps",
                                   metrics.get("verified_steps", 0))
        print(f"回放 {len(gate.ran_traces)} 条；已执行 {metrics.get('executed_steps', 0)} 步，"
              f"直接验证 {direct_steps} 步，"
              f"中介验证 {metrics.get('mediated_steps', 0)} 步；"
              f"直接一致率 {direct_text}，策略一致率 {policy_text}", flush=True)
        for report in gate.reports:
            print(f"  {report.trace_id} step{report.diverged_step} [{report.failure_type}]", flush=True)

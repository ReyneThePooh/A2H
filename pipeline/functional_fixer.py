"""功能一致性修复循环 — 差分门禁分叉报告 → LLM 修复 → 重建 → 再门禁。

对标 build_fixer.BuildFixLoop 的设计原则：
- 成败只以门禁复跑结果为准，LLM 自评不作数；
- 每轮修复后必然重建（编译错误交给 BuildFixLoop 兜底），重建后重装 HAP 再门禁；
- 同一缺陷（同轨迹/同步骤/同类型）连续两轮出现 → 说明上一轮修复无效，
  下一轮 prompt 附上一轮修复 diff 供 LLM 换思路（AI实现参考2 §7）；
- 达到 max_gate_rounds 后返回 needs_human 挂起，不阻塞流水线其余部分；
- 增量门禁通过后自动补一次出厂全量回放，兜住跨 Unit 耦合缺陷。
"""
from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any, Optional

from hello_agents.core.llm import HelloAgentsLLM

from pipeline.agents import create_pipeline_llm
from pipeline.build_fixer import BuildFixLoop
from pipeline.gate_bridge import load_plan, run_diff_gate, unit_ets_files

SYSTEM_PROMPT = """你是HarmonyOS ArkTS代码专家，负责修复Android应用翻译到HarmonyOS后\
"看起来像、功能不像"的行为缺陷：事件回调丢失、页面跳转错误、状态未更新、文案未绑定等。
修复原则：
1. 以差分测试报告中的"期望（安卓原应用行为）"为唯一正确标准
2. 最小改动：只修与报告相关的交互逻辑，不动无关代码
3. 严格遵循ArkTS规范（禁止any/unknown、禁止构造函数类型等），不得引入编译错误"""

FIX_PROMPT = """安卓原应用与鸿蒙翻译产物做差分回放测试时发现行为分叉，报告如下：

{report_md}
{repeat_context}
以下是疑似缺陷所在的 ArkTS 文件：

文件: {file_path}
```typescript
{source_code}
```

请分析分叉原因并修复该文件，使鸿蒙端行为与安卓基线一致。
要求：
- 优先检查报告中分叉步骤对应控件的事件处理、状态变量更新、页面跳转（router）逻辑
- 只修复与报告相关的逻辑，保留其余代码
- 返回修复后的完整文件，用```typescript包裹；若判断该文件与缺陷无关，只回答"与本文件无关"
"""

REPEAT_CONTEXT = """
⚠️ 注意：该缺陷在上一轮已修复过一次但仍然失败。上一轮的修复 diff 如下，
说明该思路无效，请从其他角度分析（例如：错误可能在状态管理而非事件绑定，
或页面跳转参数传递，或初始化时机）：

```diff
{previous_diff}
```
"""


class FunctionalFixLoop:
    """门禁-修复-重建-再门禁闭环。"""

    def __init__(self, llm: Optional[HelloAgentsLLM] = None,
                 max_gate_rounds: int = 3, build_fix_rounds: int = 2):
        self.llm = llm or create_pipeline_llm()
        self.max_gate_rounds = max_gate_rounds
        self.build_fix_rounds = build_fix_rounds

    def run(
        self,
        project_dir: str | Path,
        sync_dir: str | Path | None,
        workspace: str | Path,
        seeds_dir: Optional[str] = None,
        bundle: Optional[str] = None,
        device: Optional[str] = None,
        hdc_path: Optional[str] = None,
        plan_path: str | Path | None = None,
        changed_units: Optional[list[str]] = None,
        time_budget_s: float = 300.0,
    ) -> dict[str, Any]:
        """执行功能修复环。

        Args:
            project_dir: 打包后的完整 DevEco 工程
            sync_dir: 修复文件同步回的生成工程目录（同 BuildFixLoop）
            workspace: 门禁工作目录（契约文件已由 gate_bridge.prepare_workspace 生成）
            changed_units: 首轮增量选择的 Unit 集合；None → 首轮全量回放

        Returns:
            {success, rounds, gate_history, needs_human}
        """
        project_dir = Path(project_dir)
        sync_dir = Path(sync_dir) if sync_dir else None
        plan = load_plan(plan_path) if plan_path else {"units": []}

        units = list(changed_units) if changed_units else None
        prev_signatures: set[tuple] = set()
        prev_diffs: dict[tuple, str] = {}
        gate_history: list[dict] = []
        last_reports: list = []

        for round_no in range(1, self.max_gate_rounds + 1):
            print(f"\n🚦 差分门禁 第 {round_no}/{self.max_gate_rounds} 轮"
                  f"（changed_units={units or '全量'}）")
            gate = run_diff_gate(
                project_dir, workspace,
                changed_units=units or [],
                seeds_dir=seeds_dir, device=device, hdc_path=hdc_path,
                bundle=bundle, full_replay=(units is None),
                install=True, time_budget_s=time_budget_s,
            )
            gate_history.append(gate.to_dict())
            self._print_gate_summary(gate)

            if gate.passed and gate.skipped_traces:
                # 增量通过 → 出厂全量回放兜底（跨 Unit 耦合缺陷）
                print("  增量门禁通过，执行出厂全量回放...")
                gate = run_diff_gate(
                    project_dir, workspace, changed_units=[],
                    seeds_dir=seeds_dir, device=device, hdc_path=hdc_path,
                    bundle=bundle, full_replay=True,
                    install=False, time_budget_s=time_budget_s,
                )
                gate_history.append(gate.to_dict())
                self._print_gate_summary(gate)

            if gate.passed:
                return {"success": True, "rounds": round_no,
                        "gate_history": gate_history, "needs_human": []}

            last_reports = gate.reports
            if round_no == self.max_gate_rounds:
                print(f"⚠️ 已达门禁修复轮数上限 ({self.max_gate_rounds})，"
                      "剩余缺陷挂起待人工介入")
                break

            # ---- LLM 修复 ------------------------------------------------------
            fixed_any, round_diffs = self._fix_reports(
                project_dir, sync_dir, plan, gate.reports,
                prev_signatures, prev_diffs,
            )
            if not fixed_any:
                print("⚠️ 本轮没有产生任何有效修复，提前结束")
                break

            # ---- 重建（编译错误交给 BuildFixLoop 兜底） -------------------------
            print("\n🔨 修复后重建...")
            build = BuildFixLoop(max_fix_rounds=self.build_fix_rounds).run(
                project_dir, sync_dir=sync_dir)
            if not build["success"]:
                print("❌ 功能修复引入的代码无法通过构建，终止门禁环")
                return {"success": False, "rounds": round_no,
                        "gate_history": gate_history,
                        "needs_human": [r.to_dict() for r in last_reports],
                        "build_failed": True}

            prev_signatures = {r.signature() for r in gate.reports}
            prev_diffs = round_diffs
            units = self._next_units(gate.reports)

        return {"success": False, "rounds": self.max_gate_rounds,
                "gate_history": gate_history,
                "needs_human": [r.to_dict() for r in last_reports]}

    # ------------------------------------------------------------------
    # 单轮修复
    # ------------------------------------------------------------------

    def _fix_reports(
        self,
        project_dir: Path,
        sync_dir: Optional[Path],
        plan: dict,
        reports: list,
        prev_signatures: set[tuple],
        prev_diffs: dict[tuple, str],
    ) -> tuple[bool, dict[tuple, str]]:
        """按报告逐一修复疑似文件，返回 (是否有有效修复, 本轮 diff 记录)。"""
        fixed_any = False
        round_diffs: dict[tuple, str] = {}

        for report in reports:
            if report.failure_type == "TIMEOUT":
                print(f"  ⏱ {report.trace_id}: 超时类缺陷无法自动修复，跳过")
                continue

            # 崩溃栈直接定位的文件最权威，优先于 unit 反查
            stack_files = [
                project_dir / rel
                for rel in getattr(report, "suspect_files", [])
            ]
            stack_files = [p for p in stack_files if p.exists()]
            if stack_files:
                files = stack_files
                print(f"  🎯 {report.trace_id}: 崩溃栈定位到 "
                      f"{[p.name for p in files]}")
            else:
                files = unit_ets_files(report.suspect_units, plan, project_dir)
            if not files:
                pages_dir = project_dir / "entry" / "src" / "main" / "ets" / "pages"
                files = sorted(pages_dir.glob("*.ets")) if pages_dir.exists() else []
                if files:
                    print(f"  ⚠️ {report.trace_id}: 未定位到疑似 Unit 文件，"
                          f"退化为全部页面文件 ({len(files)} 个)")
            if not files:
                print(f"  ⚠️ {report.trace_id}: 找不到可修复的 .ets 文件，跳过")
                continue

            repeat_context = ""
            sig = report.signature()
            if sig in prev_signatures:
                previous = prev_diffs.get(sig, "(上一轮 diff 缺失)")
                repeat_context = REPEAT_CONTEXT.format(previous_diff=previous)
                print(f"  🔁 {report.trace_id}: 与上一轮同一缺陷，附上一轮修复 diff")

            report_md = report.to_prompt()
            diffs: list[str] = []
            for fp in files:
                diff = self._fix_file(fp, report_md, repeat_context)
                if diff:
                    fixed_any = True
                    diffs.append(diff)
                    if sync_dir:
                        rel = fp.relative_to(project_dir)
                        target = sync_dir / rel
                        if target.exists():
                            target.write_text(
                                fp.read_text(encoding="utf-8"), encoding="utf-8")
                            print(f"      ↩ 已同步回生成工程: {target}")
            if diffs:
                round_diffs[sig] = "\n".join(diffs)[:4000]

        return fixed_any, round_diffs

    def _fix_file(self, fp: Path, report_md: str, repeat_context: str) -> Optional[str]:
        """修复单个文件，写盘并返回 unified diff；无有效修改返回 None。"""
        rel_name = fp.name
        print(f"    📄 修复 {rel_name} ...")
        try:
            source = fp.read_text(encoding="utf-8")
        except OSError as e:
            print(f"      ❌ 读取失败: {e}")
            return None

        prompt = FIX_PROMPT.format(
            report_md=report_md, repeat_context=repeat_context,
            file_path=rel_name, source_code=source,
        )
        response = self._invoke(prompt)
        if "与本文件无关" in response:
            print("      · LLM 判断与本文件无关")
            return None
        fixed = BuildFixLoop._extract_code(response)
        if not fixed or fixed == source:
            print("      ⚠️ 未产生有效修改")
            return None
        reject = BuildFixLoop._validate_fixed(source, fixed)
        if reject:
            print(f"      ⚠️ 修复结果校验不通过，已丢弃: {reject}")
            return None

        backup = fp.with_suffix(fp.suffix + ".funcbak")
        if not backup.exists():
            backup.write_text(source, encoding="utf-8")
        fp.write_text(fixed, encoding="utf-8")
        print("      ✅ 已写入修复")
        return "\n".join(difflib.unified_diff(
            source.splitlines(), fixed.splitlines(),
            fromfile=f"a/{rel_name}", tofile=f"b/{rel_name}", lineterm="",
        ))

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _invoke(self, prompt: str) -> str:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        try:
            return self.llm.invoke(messages) or ""
        except Exception as e:
            print(f"      ⚠️ LLM调用失败: {e}")
            return ""

    @staticmethod
    def _next_units(reports: list) -> Optional[list[str]]:
        """下一轮增量选择的 Unit 集合；任一报告缺 suspect_units → None（全量）。"""
        units: set[str] = set()
        for r in reports:
            if r.failure_type == "TIMEOUT":
                continue
            if not r.suspect_units:
                return None
            units.update(r.suspect_units)
        return sorted(units) if units else None

    @staticmethod
    def _print_gate_summary(gate) -> None:
        status = "✅ 通过" if gate.passed else f"❌ 未通过（{len(gate.reports)} 个缺陷）"
        print(f"  门禁第 {gate.round_no} 轮: {status}  "
              f"回放 {len(gate.ran_traces)} 条 / 跳过 {len(gate.skipped_traces)} 条  "
              f"一致率 {gate.consistency_rate:.0%}  耗时 {gate.elapsed_s:.0f}s")
        for r in gate.reports:
            print(f"    - {r.trace_id} step{r.diverged_step} "
                  f"[{r.failure_type}] 疑似 {r.suspect_units or '未定位'}")
        if gate.flaky:
            print(f"    ~ FLAKY 观察名单: {gate.flaky}")

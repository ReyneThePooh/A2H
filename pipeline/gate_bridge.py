"""差分测试门禁 ↔ 翻译流水线桥接层（AI实现参考2 §3 集成契约的 A2H 侧）。

职责：
1. 从 .pipeline_cache/translation_plan.json 推导 unit_page_map.json
   （Unit → Android 页面 → Harmony 页面）；
2. 按翻译器命名约定（XxxActivity → pages/XxxPage，见
   unit_translator._guess_filename）生成 page_pairs.json；
3. 定位构建产物 HAP、读取 bundleName，组装 GateRequest 调用 run_gate。

方向约束（验收 §9 隔离性）：流水线 import 门禁（diff_tester.gate），
门禁不 import 流水线内部模块，只消费 workspace 下的契约 JSON 文件。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterator, Optional

from diff_tester.gate import GateRequest, GateResult, run_gate

DEFAULT_ABILITY = "EntryAbility"

#: 翻译产物页面目录（相对打包工程根）
PAGES_REL = Path("entry/src/main/ets/pages")


# ---------------------------------------------------------------------------
# translation_plan.json → 契约文件
# ---------------------------------------------------------------------------

def load_plan(plan_path: str | Path) -> dict:
    with open(plan_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _activity_entries(plan: dict) -> Iterator[tuple[str, str, str]]:
    """遍历计划中的 Activity 源文件，产出 (unit_name, activity_stem, package)。"""
    for unit in plan.get("units", []):
        for src in unit.get("sources", []):
            src_norm = str(src).replace("\\", "/")
            if not src_norm.endswith(".java"):
                continue
            stem = Path(src_norm).stem
            if not stem.endswith("Activity"):
                continue
            package = ""
            if "/java/" in src_norm:
                pkg_path = src_norm.rsplit("/java/", 1)[1]
                package = ".".join(Path(pkg_path).parent.parts)
            yield unit.get("name", ""), stem, package


def page_for_activity(stem: str) -> str:
    """命名约定：MainActivity → MainPage（与 unit_translator._guess_filename 一致）。"""
    return stem[:-len("Activity")] + "Page"


def build_unit_page_map(
    plan_path: str | Path,
    out_path: str | Path,
    ability: str = DEFAULT_ABILITY,
) -> dict:
    """生成 Unit → 页面集合映射（门禁增量选择的依据）。"""
    plan = load_plan(plan_path)
    mapping: dict[str, dict] = {}
    for unit in plan.get("units", []):
        mapping[unit.get("name", "")] = {"android_pages": [], "harmony_pages": []}
    for unit_name, stem, _pkg in _activity_entries(plan):
        entry = mapping[unit_name]
        entry["android_pages"].append(stem)
        entry["harmony_pages"].append(f"{ability}:pages/{page_for_activity(stem)}")
    mapping["__global__"] = {"android_pages": ["*"], "harmony_pages": ["*"]}

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(mapping, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    return mapping


def build_page_pairs(
    plan_path: str | Path,
    out_path: str | Path,
    ability: str = DEFAULT_ABILITY,
) -> dict:
    """生成安卓页面 ↔ 鸿蒙页面对应表。

    翻译器知道每个 Activity 译成了哪个 Page（命名约定），因此直接产出；
    每个 Activity 写三种键形（简名 / .简名 / 全限定名），与安卓端
    dumpsys 可能报告的形式都能精确命中。out_path 已存在时保留人工条目
    （自动生成的键不覆盖已有键）。
    """
    plan = load_plan(plan_path)
    pairs: dict[str, str] = {}
    for _unit, stem, pkg in _activity_entries(plan):
        harmony = f"{ability}:pages/{page_for_activity(stem)}"
        pairs.setdefault(stem, harmony)
        pairs.setdefault(f".{stem}", harmony)
        if pkg:
            pairs.setdefault(f"{pkg}.{stem}", harmony)

    out_path = Path(out_path)
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                pairs.update(existing)     # 人工维护的条目优先
        except (OSError, json.JSONDecodeError):
            pass
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(pairs, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    return pairs


def prepare_workspace(
    workspace: str | Path,
    plan_path: str | Path,
    ability: str = DEFAULT_ABILITY,
) -> None:
    """生成/刷新门禁 workspace 下的两份契约文件（G3：自动生成）。"""
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    build_unit_page_map(plan_path, workspace / "unit_page_map.json", ability)
    build_page_pairs(plan_path, workspace / "page_pairs.json", ability)


# ---------------------------------------------------------------------------
# 工程侧信息：bundleName / HAP / Unit 对应的 .ets 文件
# ---------------------------------------------------------------------------

_BUNDLE_RE = re.compile(r'"bundleName"\s*:\s*"([^"]+)"')


def read_bundle_name(project_dir: str | Path) -> Optional[str]:
    """从打包工程 AppScope/app.json5 读取 bundleName。"""
    app_json = Path(project_dir) / "AppScope" / "app.json5"
    if not app_json.exists():
        return None
    m = _BUNDLE_RE.search(app_json.read_text(encoding="utf-8", errors="replace"))
    return m.group(1) if m else None


def find_hap(project_dir: str | Path) -> Optional[Path]:
    """定位构建产物 HAP（优先签名包 *-signed.hap）。"""
    build_dir = Path(project_dir) / "entry" / "build"
    if not build_dir.exists():
        return None
    haps = sorted(build_dir.rglob("*.hap"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    if not haps:
        return None
    signed = [p for p in haps if "signed" in p.name.lower()]
    return signed[0] if signed else haps[0]


def unit_ets_files(
    unit_names: list[str],
    plan: dict,
    project_dir: str | Path,
) -> list[Path]:
    """疑似 Unit → 打包工程内对应的 .ets 文件（修复环的改动目标）。

    命名约定同 unit_translator._guess_filename：
    XxxActivity.java → XxxPage.ets；其他 Java 文件 → 同名 .ets。
    """
    pages_dir = Path(project_dir) / PAGES_REL
    wanted = set(unit_names)
    files: list[Path] = []
    for unit in plan.get("units", []):
        if unit.get("name", "") not in wanted:
            continue
        for src in unit.get("sources", []):
            src_norm = str(src).replace("\\", "/")
            if not src_norm.endswith(".java"):
                continue
            stem = Path(src_norm).stem
            name = (page_for_activity(stem) if stem.endswith("Activity") else stem)
            candidate = pages_dir / f"{name}.ets"
            if candidate.exists() and candidate not in files:
                files.append(candidate)
    return files


# ---------------------------------------------------------------------------
# 门禁调用入口（流水线侧）
# ---------------------------------------------------------------------------

def run_diff_gate(
    project_dir: str | Path,
    workspace: str | Path,
    changed_units: list[str],
    seeds_dir: Optional[str] = None,
    device: Optional[str] = None,
    hdc_path: Optional[str] = None,
    bundle: Optional[str] = None,
    full_replay: bool = False,
    install: bool = True,
    time_budget_s: float = 300.0,
) -> GateResult:
    """组装 GateRequest 并执行一轮门禁。

    Args:
        project_dir: 打包后的完整 DevEco 工程（HAP 从其 build 目录定位）
        install: True 时先 hdc install 最新 HAP（重建后必须重装）
    """
    project_dir = Path(project_dir)
    bundle = bundle or read_bundle_name(project_dir)
    if not bundle:
        raise RuntimeError(
            f"无法从 {project_dir / 'AppScope' / 'app.json5'} 读取 bundleName，"
            "请用 --bundle 显式指定"
        )
    hap: Optional[Path] = None
    if install:
        hap = find_hap(project_dir)
        if hap is None:
            raise RuntimeError(f"未在 {project_dir} 下找到 .hap 构建产物，请先构建")

    return run_gate(GateRequest(
        bundle=bundle,
        workspace=str(workspace),
        changed_units=list(changed_units),
        hap_path=str(hap) if hap else None,
        harmony_device=device,
        hdc_path=hdc_path,
        full_replay=full_replay,
        seeds_dir=seeds_dir,
        time_budget_s=time_budget_s,
    ))

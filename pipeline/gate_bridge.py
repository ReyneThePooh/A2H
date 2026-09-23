"""翻译与差分门禁的桥接层。

实际运行使用真实产物的页面映射，部署前验证构建输入和 HAP 哈希。
旧工程可使用明确的映射文件；命名推导仅保留为独立兼容工具。
"""
from __future__ import annotations

import json
import hashlib
import re
from pathlib import Path
from typing import Iterator, Optional

from run_control import atomic_write_json

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
    plan_path: str | Path | None,
    ability: str = DEFAULT_ABILITY,
    project_dir: str | Path | None = None,
) -> None:
    """准备页面映射，不把应用结构正确当成运行差分测试的前提。

    有清单时使用真实映射；无清单的旧工程要求已有明确映射。
    已有映射与清单冲突时停止并保留文件，避免静默改变测试预期。
    """
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    if project_dir is not None:
        from pipeline.artifacts import load_artifact_manifest, project_path
        root = Path(project_dir).resolve()
        pairs_path, units_path = workspace / "page_pairs.json", workspace / "unit_page_map.json"
        if not (root / "translation_manifest.json").is_file():
            if not pairs_path.is_file() or not units_path.is_file():
                raise RuntimeError("PAGE_MAPPING_MISSING: 旧工程需要真实产物清单，或已有 page_pairs.json 与 unit_page_map.json")
            _validate_mappings(load_plan(pairs_path), load_plan(units_path), root)
            return
        manifest = load_artifact_manifest(root)
        mapping = {u["name"]: {"android_pages": [], "harmony_pages": [],
                              "unit_id": u["unit_id"]} for u in manifest["units"]}
        by_id = {u["unit_id"]: u["name"] for u in manifest["units"]}
        pairs = {}
        for page in manifest["pages"]:
            output = project_path(root, page["output"])
            route_output = project_path(root, f"entry/src/main/ets/{page['route']}.ets")
            if not output.is_file() or output != route_output or page["unit_id"] not in by_id:
                raise RuntimeError(f"PAGE_MAPPING_INVALID: {page['android_activity']}")
            android = page["android_activity"]
            harmony = f"{ability}:{page['route']}"
            for key in (android, android.rsplit(".", 1)[-1], "." + android.rsplit(".", 1)[-1]):
                if key in pairs and pairs[key] != harmony:
                    raise RuntimeError(f"AMBIGUOUS_PAGE_MAPPING: {key}")
                pairs[key] = harmony
            unit = mapping[by_id[page["unit_id"]]]
            unit["android_pages"].append(android)
            unit["harmony_pages"].append(harmony)
        mapping["__global__"] = {"android_pages": ["*"], "harmony_pages": ["*"]}
        _validate_mappings(pairs, mapping, root)
        for path, expected in ((pairs_path, pairs), (units_path, mapping)):
            if path.is_file() and load_plan(path) != expected:
                raise RuntimeError(f"PAGE_MAPPING_CONFLICT: {path} 与清单不一致，已有映射已保留")
        atomic_write_json(units_path, mapping)
        atomic_write_json(pairs_path, pairs)
        return
    if plan_path is None:
        raise RuntimeError("PAGE_MAPPING_MISSING: 缺少工程或翻译计划")
    build_unit_page_map(plan_path, workspace / "unit_page_map.json", ability)
    build_page_pairs(plan_path, workspace / "page_pairs.json", ability)


def prepare_static_index(
    project_dir: str | Path,
    workspace: str | Path,
    seeds_dir: str | Path | None = None,
) -> tuple[object, list[dict]]:
    """Index ArkTS sources and validate replay-facing static contracts.

    The index is persisted beside the gate evidence so repair and diagnostic
    tools can cite exact source locations.  Static issues are reported before
    device deployment; a missing action id or dangling literal route is a
    deterministic translation contract failure and must not be converted into
    a runtime replay failure.
    """
    from analyzers.arkts_index import (
        build_arkts_index, save_arkts_index, validate_static_contract,
    )

    root = Path(project_dir).resolve()
    work = Path(workspace).resolve()
    work.mkdir(parents=True, exist_ok=True)
    index = build_arkts_index(root)
    save_arkts_index(index, work / "arkts_index.json")

    traces: list[dict] = []
    if seeds_dir:
        seed_root = Path(seeds_dir)
        for path in sorted(seed_root.glob("*.json"), key=lambda item: item.name.casefold()):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                # Trace schema validation remains the gate's source of truth;
                # malformed seeds must not be silently treated as static pass.
                continue
            if isinstance(payload, dict):
                traces.append(payload)
    pairs: dict = {}
    pairs_path = work / "page_pairs.json"
    if pairs_path.is_file():
        try:
            loaded = json.loads(pairs_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                pairs = loaded
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
    issues = validate_static_contract(index, pairs, traces)
    atomic_write_json(work / "arkts_static_issues.json", {"issues": issues})
    if issues:
        preview = json.dumps(issues[:8], ensure_ascii=False, separators=(",", ":"))
        raise RuntimeError(
            f"STATIC_CONTRACT_INVALID: {len(issues)} issue(s); see "
            f"{work / 'arkts_static_issues.json'}; preview={preview}"
        )
    return index, issues


def _validate_mappings(pairs: dict, mapping: dict, root: Path) -> None:
    from pipeline.artifacts import project_path
    if not isinstance(pairs, dict) or not pairs or not isinstance(mapping, dict) or not mapping:
        raise RuntimeError("PAGE_MAPPING_INVALID: 页面与单元映射必须为非空对象")
    for android, harmony in pairs.items():
        if not isinstance(android, str) or not android or not isinstance(harmony, str):
            raise RuntimeError("PAGE_MAPPING_INVALID: 页面映射必须为非空字符串")
        ability, separator, route = harmony.partition(":")
        if (not ability or not separator or not route
                or not project_path(root, f"entry/src/main/ets/{route}.ets").is_file()):
            raise RuntimeError(f"PAGE_MAPPING_INVALID: {android} -> {harmony}")
    for unit in mapping.values():
        if not isinstance(unit, dict) or any(
            not isinstance(unit.get(key), list)
            or any(not isinstance(page, str) for page in unit[key])
            for key in ("android_pages", "harmony_pages")
        ):
            raise RuntimeError("PAGE_MAPPING_INVALID: 单元映射需要页面列表")


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
    signed = [p for p in haps if p.name.lower().endswith("-signed.hap")]
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
    root = Path(project_dir)
    if (root / "translation_manifest.json").exists():
        from pipeline.artifacts import load_artifact_manifest
        manifest = load_artifact_manifest(root)
        selected = {u["unit_id"] for u in manifest["units"]
                    if u["name"] in unit_names or u["unit_id"] in unit_names}
        return [root / out["path"] for out in manifest["outputs"]
                if out["unit_id"] in selected and (root / out["path"]).is_file()
                and out["path"].endswith(".ets")]
    pages_dir = root / PAGES_REL
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
    time_budget_s: Optional[float] = None,
    confirm_failures: bool = True,
    trace_ids: Optional[list[str]] = None,
    static_preflight: bool = True,
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
    deployment = {}
    if install:
        metadata_path = project_dir / ".pipeline_build.json"
        if not metadata_path.exists():
            raise RuntimeError("BUILD_PROVENANCE_MISSING: rebuild before replay")
        deployment = json.loads(metadata_path.read_text(encoding="utf-8"))
        if deployment.get("status") != "success":
            raise RuntimeError("BUILD_NOT_VERIFIED: latest build did not pass")
        from pipeline.artifacts import project_source_fingerprint
        if deployment.get("project_input_sha256") != project_source_fingerprint(project_dir):
            raise RuntimeError("STALE_BUILD: project changed since the recorded build")
        artifacts = deployment.get("artifacts", [])
        artifacts = sorted(artifacts, key=lambda x: not bool(x.get("signed")))
        if not artifacts:
            raise RuntimeError("BUILD_ARTIFACT_MISSING: no verified HAP")
        artifact = artifacts[0]
        hap = (project_dir / artifact["path"]).resolve()
        if not hap.is_relative_to(project_dir.resolve()) or not hap.is_file():
            raise RuntimeError("BUILD_ARTIFACT_MISSING: invalid HAP path")
        if hashlib.sha256(hap.read_bytes()).hexdigest() != artifact["sha256"]:
            raise RuntimeError("STALE_BUILD: HAP digest mismatch")
        deployment = {**deployment, "selected_artifact": artifact}

    if static_preflight:
        effective_seeds = seeds_dir or (Path(workspace) / "seeds")
        prepare_static_index(
            project_dir,
            workspace,
            effective_seeds if Path(effective_seeds).is_dir() else None,
        )

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
        deployment_metadata=deployment,
        confirm_failures=confirm_failures,
        trace_ids=list(trace_ids or []),
    ))

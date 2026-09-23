"""Import an existing translated project without regenerating its source code.

The import descriptor is explicit: every Android translation unit owns a fixed
set of existing ArkTS outputs, and every Android activity maps to an existing
output.  Importing writes provenance metadata only.  Any mismatch between the
declared mapping and the actual project is reported for correction before
replay; importing never edits application source.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline.artifacts import (
    SCHEMA_VERSION,
    content_hash,
    output_role,
    project_path,
    stable_unit_id,
    write_artifact_manifest,
)
from pipeline.gate_bridge import load_plan
from pipeline.resource_migrator import parse_manifest


def import_legacy_project(
    project_dir: str | Path,
    source_root: str | Path,
    plan_path: str | Path,
    descriptor_path: str | Path,
) -> dict:
    """Create a provenance manifest for an existing translated project.

    The descriptor has three required fields:
      - ``unit_outputs``: translation-unit name -> existing output paths
      - ``activity_pages``: fully-qualified Android activity -> output path
      - ``launcher_activity``: fully-qualified Android launcher activity

    Output paths may be filenames relative to ``entry/src/main/ets/pages`` or
    project-relative paths.  The descriptor may declare a component output as
    a page.  That discrepancy is intentionally retained as contract evidence
    for inspection; this function never edits application source.
    """
    project = Path(project_dir).resolve()
    source = Path(source_root).resolve()
    descriptor = json.loads(Path(descriptor_path).read_text(encoding="utf-8"))
    plan = load_plan(plan_path)
    units = plan.get("units")
    if not isinstance(units, list) or not units:
        raise ValueError("Legacy import requires a plan with translation units")

    unit_outputs = descriptor.get("unit_outputs")
    activity_pages = descriptor.get("activity_pages")
    launcher_activity = descriptor.get("launcher_activity")
    if not isinstance(unit_outputs, dict) or not isinstance(activity_pages, dict):
        raise ValueError("Legacy import descriptor is missing unit_outputs or activity_pages")
    if not isinstance(launcher_activity, str) or not launcher_activity:
        raise ValueError("Legacy import descriptor is missing launcher_activity")

    planned_names = {unit.get("name") for unit in units}
    if set(unit_outputs) != planned_names:
        missing = sorted(str(name) for name in planned_names - set(unit_outputs))
        extra = sorted(str(name) for name in set(unit_outputs) - planned_names)
        raise ValueError(f"Legacy unit mapping mismatch; missing={missing}, extra={extra}")

    android_manifest = parse_manifest(str(_find_android_manifest(source)))
    required = [item["name"] for item in android_manifest.get("activities", [])]
    if set(activity_pages) != set(required):
        missing = sorted(set(required) - set(activity_pages))
        extra = sorted(set(activity_pages) - set(required))
        raise ValueError(f"Legacy activity mapping mismatch; missing={missing}, extra={extra}")
    launchers = android_manifest.get("launchers", [])
    if len(launchers) != 1 or launchers[0].get("activity") != launcher_activity:
        raise ValueError("Legacy launcher mapping disagrees with AndroidManifest.xml")

    normalized_outputs: dict[str, list[str]] = {}
    owners: dict[str, str] = {}
    for unit in units:
        name = unit["name"]
        values = unit_outputs[name]
        if not isinstance(values, list) or not values:
            raise ValueError(f"Legacy unit has no outputs: {name}")
        normalized_outputs[name] = []
        for value in values:
            relative = _normalize_output(value)
            path = project_path(project, relative)
            if not path.is_file():
                raise FileNotFoundError(path)
            key = relative.casefold()
            if key in owners:
                raise ValueError(f"Legacy output has multiple owners: {relative}")
            owners[key] = name
            normalized_outputs[name].append(relative)

    actual = {
        path.relative_to(project).as_posix().casefold(): path.relative_to(project).as_posix()
        for path in (project / "entry/src/main/ets/pages").rglob("*.ets")
    }
    if set(owners) != set(actual):
        missing = sorted(actual[key] for key in set(actual) - set(owners))
        extra = sorted(key for key in set(owners) - set(actual))
        raise ValueError(f"Legacy output inventory mismatch; unowned={missing}, unknown={extra}")

    page_outputs = {_normalize_output(value) for value in activity_pages.values()}
    if len(page_outputs) != len(activity_pages):
        raise ValueError("Each Android activity must map to a distinct legacy page output")
    if any(value.casefold() not in owners for value in page_outputs):
        raise ValueError("Legacy activity maps to an unowned output")

    data = {
        "schema_version": SCHEMA_VERSION,
        "status": "generated",
        "source_root": str(source),
        "imported_legacy": True,
        "units": [],
        "outputs": [],
        "pages": [],
        "required_activities": required,
        "launcher": None,
        "issues": [],
    }
    unit_by_output: dict[str, dict] = {}
    for unit in units:
        sources = list(unit.get("sources", []))
        if not sources:
            raise ValueError(f"Legacy unit has no sources: {unit.get('name')}")
        uid = stable_unit_id(sources)
        outputs = normalized_outputs[unit["name"]]
        unit_record = {
            "unit_id": uid,
            "name": unit["name"],
            "sources": sources,
            "status": "generated",
            "outputs": outputs,
        }
        data["units"].append(unit_record)
        for relative in outputs:
            path = project / relative
            declared_page = relative in page_outputs
            role = "page" if declared_page else output_role(path.read_text(encoding="utf-8"))
            route = Path(relative).relative_to("entry/src/main/ets").with_suffix("").as_posix() if role == "page" else None
            record = {
                "path": relative,
                "unit_id": uid,
                "unit_name": unit["name"],
                "sources": sources,
                "role": role,
                "route": route,
                "sha256": content_hash(path.read_bytes()),
            }
            data["outputs"].append(record)
            unit_by_output[relative.casefold()] = record

    for activity in required:
        relative = _normalize_output(activity_pages[activity])
        output = unit_by_output[relative.casefold()]
        source_candidates = [item for item in output["sources"] if Path(item).stem == activity.rsplit(".", 1)[-1]]
        if len(source_candidates) != 1:
            raise ValueError(f"Legacy page has no unambiguous Android activity source: {activity}")
        data["pages"].append({
            "android_activity": activity,
            "source": source_candidates[0],
            "output": output["path"],
            "route": output["route"],
            "unit_id": output["unit_id"],
        })

    launcher_page = next(page for page in data["pages"] if page["android_activity"] == launcher_activity)
    data["launcher"] = {
        "android_activity": launcher_activity,
        "component": launchers[0]["component"],
        "route": launcher_page["route"],
        "output": launcher_page["output"],
    }
    write_artifact_manifest(project, data)
    return data


def _normalize_output(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Legacy output paths must be nonempty strings")
    normalized = value.replace("\\", "/").removeprefix("./")
    if "/" not in normalized:
        normalized = "entry/src/main/ets/pages/" + normalized
    if not normalized.endswith(".ets"):
        normalized += ".ets"
    return normalized


def _find_android_manifest(source: Path) -> Path:
    for relative in ("app/src/main/AndroidManifest.xml", "src/main/AndroidManifest.xml", "AndroidManifest.xml"):
        candidate = source / relative
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"AndroidManifest.xml not found under {source}")

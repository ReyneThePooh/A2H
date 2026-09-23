"""Versioned facts linking source units, generated files and runnable routes.

This is a translation contract, not a claim of behavioral equivalence.  Generated
and incomplete outputs are deliberately distinct from runtime-validated builds.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from copy import deepcopy
from pathlib import Path, PurePosixPath
from typing import Mapping

SCHEMA_VERSION = 1
MANIFEST_NAME = "translation_manifest.json"

_BUILD_CACHE_DIRS = frozenset({
    "build", ".hvigor", ".git", "oh_modules", "node_modules",
    "__pycache__", ".pytest_cache",
})
_ROOT_TOOL_DIRS = frozenset({
    ".pipeline_cache", ".pipeline_transactions", ".diff_gate", ".idea",
    "record_out", "seeds", "tests", "logs", "transactions", "candidates",
})
_ROOT_TOOL_FILES = frozenset({
    MANIFEST_NAME, "oracle.py", "oracle.json", "page_pairs.json",
    "unit_page_map.json",
})
_ROOT_TRANSIENT_SUFFIXES = (".log", ".tmp", ".lock", ".hap", ".hsp")
_GENERATED_SUFFIXES = (".bak", ".funcbak", ".pyc")
_GENERATED_PREFIXES = (".run-tmp-", ".run-tx-")


class ArtifactContractError(ValueError):
    """Required provenance or structural contract is missing or inconsistent."""


def content_hash(data: str | bytes) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def stable_unit_id(sources: list[str]) -> str:
    return "unit_" + content_hash("\n".join(sorted(s.replace("\\", "/") for s in sources)))[:20]


def _excluded_from_project_fingerprint(relative: Path) -> bool:
    parts = relative.parts
    directory_names = {part.casefold() for part in parts[:-1]}
    if directory_names.intersection(_BUILD_CACHE_DIRS):
        return True

    root_name = parts[0].casefold()
    if len(parts) > 1 and (
        root_name in _ROOT_TOOL_DIRS or root_name.startswith(".pipeline")
    ):
        return True

    name = relative.name.casefold()
    if name.startswith(_GENERATED_PREFIXES) or name.endswith(_GENERATED_SUFFIXES):
        return True
    if len(parts) == 1 and (
        name in _ROOT_TOOL_FILES
        or name.startswith((".pipeline", ".env"))
        or name.endswith(_ROOT_TRANSIENT_SUFFIXES)
    ):
        return True
    return False


def project_source_fingerprint(project_dir: str | Path) -> str:
    """Hash build inputs while excluding known generated and root tool state."""
    root = Path(project_dir).resolve()
    records = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if not path.is_file() or _excluded_from_project_fingerprint(relative):
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ArtifactContractError(f"Build input escapes project: {relative}")
        # rawfile/resources may use any extension (fonts, audio, binary assets).
        # Hash every managed file rather than a source-language suffix whitelist.
        records.append((relative.as_posix(), content_hash(path.read_bytes())))
    return content_hash(json.dumps(records, ensure_ascii=False))


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def project_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str):
        raise ArtifactContractError("Artifact path must be a string")
    normalized = relative.replace("\\", "/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts or ":" in normalized:
        raise ArtifactContractError(f"Unsafe artifact path: {relative}")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ArtifactContractError(f"Artifact escapes project: {relative}")
    return resolved


_LEXEMES = re.compile(r"//[^\n]*|/\*[\s\S]*?\*/|'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`")


def code_without_comments(code: str, *, mask_strings: bool = False) -> str:
    def replace(match: re.Match) -> str:
        value = match.group()
        if value.startswith(("//", "/*")) or mask_strings:
            return "".join("\n" if c == "\n" else " " for c in value)
        return value
    return _LEXEMES.sub(replace, code)


def output_role(code: str) -> str:
    stripped = code_without_comments(code, mask_strings=True)
    if re.search(r"(?m)^\s*@Entry\b", stripped):
        return "page"
    if re.search(r"(?m)^\s*@Component(?:V2)?\b", stripped):
        return "component"
    return "module"


def load_artifact_manifest(project_dir: str | Path) -> dict:
    path = Path(project_dir) / MANIFEST_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ArtifactContractError(f"Missing or unreadable translation manifest: {path}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise ArtifactContractError(f"Unsupported translation manifest schema: {path}")
    if not isinstance(data.get("status"), str):
        raise ArtifactContractError("Manifest status must be a string")
    for key in ("units", "outputs", "pages", "required_activities"):
        if not isinstance(data.get(key), list):
            raise ArtifactContractError(f"Invalid manifest field: {key}")
    if any(not isinstance(item, dict) for key in ("units", "outputs", "pages") for item in data[key]):
        raise ArtifactContractError("Manifest entries must be objects")
    if any(not isinstance(name, str) or not name for name in data["required_activities"]):
        raise ArtifactContractError("Required activities must be nonempty strings")
    if not isinstance(data.get("issues", []), list):
        raise ArtifactContractError("Manifest issues must be an array")
    for unit in data["units"]:
        if (not isinstance(unit.get("unit_id"), str) or not isinstance(unit.get("name"), str)
                or not isinstance(unit.get("status"), str)
                or not isinstance(unit.get("sources"), list) or not unit["sources"]
                or any(not isinstance(source, str) for source in unit["sources"])
                or not isinstance(unit.get("outputs"), list)
                or any(not isinstance(path, str) for path in unit["outputs"])):
            raise ArtifactContractError("Invalid unit schema")
    for output in data["outputs"]:
        if (not isinstance(output.get("path"), str) or not isinstance(output.get("unit_id"), str)
                or not isinstance(output.get("sources"), list) or not output["sources"]
                or any(not isinstance(source, str) for source in output["sources"])
                or not isinstance(output.get("role"), str) or output["role"] not in {"page", "component", "module"}
                or not isinstance(output.get("sha256"), str)
                or (output.get("route") is not None and not isinstance(output["route"], str))):
            raise ArtifactContractError("Invalid output schema")
        project_path(Path(project_dir), output["path"])
    for page in data["pages"]:
        if any(not isinstance(page.get(key), str) or not page[key]
               for key in ("android_activity", "source", "output", "route", "unit_id")):
            raise ArtifactContractError("Invalid page mapping schema")
    launcher = data.get("launcher")
    if launcher is not None and (not isinstance(launcher, dict) or any(not isinstance(launcher.get(key), str)
                                 for key in ("android_activity", "component", "route", "output"))):
        raise ArtifactContractError("Invalid launcher schema")
    return data


def write_artifact_manifest(project_dir: str | Path, manifest: dict) -> None:
    atomic_write_json(Path(project_dir) / MANIFEST_NAME, manifest)


def refresh_artifact_hashes(project_dir: str | Path) -> dict:
    root = Path(project_dir).resolve()
    manifest = updated_artifact_manifest(root, {})
    write_artifact_manifest(root, manifest)
    return manifest


def updated_artifact_manifest(
    project_dir: str | Path,
    replacements: Mapping[Path, bytes],
) -> dict:
    """Compute output hashes for a prospective source batch without writing it."""
    root = Path(project_dir).resolve()
    manifest = deepcopy(load_artifact_manifest(root))
    staged = {Path(path).resolve(): data for path, data in replacements.items()}
    for output in manifest["outputs"]:
        path = project_path(root, output["path"])
        if path in staged:
            output["sha256"] = content_hash(staged[path])
        elif path.is_file():
            output["sha256"] = content_hash(path.read_bytes())
    return manifest


def validate_project_contract(project_dir: str | Path, *, verify_hashes: bool = True) -> list[dict]:
    root = Path(project_dir)
    issues: list[dict] = []

    def add(code: str, file: str, message: str) -> None:
        issues.append({"code": code, "file": file, "message": message})

    try:
        data = load_artifact_manifest(root)
    except ArtifactContractError as exc:
        return [{"code": "CONTRACT_MANIFEST", "file": MANIFEST_NAME, "message": str(exc)}]
    if data.get("status") not in {"generated", "validated"}:
        add("CONTRACT_INCOMPLETE", MANIFEST_NAME, "Translation has incomplete or failed units")
    for issue in data.get("issues", []):
        if isinstance(issue, dict):
            add(str(issue.get("code", "CONTRACT_SOURCE")), str(issue.get("file", "")), str(issue.get("message", "")))
    outputs: dict[str, dict] = {}
    routes: dict[str, str] = {}
    units = {}
    for unit in data["units"]:
        uid = unit["unit_id"]
        if uid != stable_unit_id(unit["sources"]) or uid in units:
            add("CONTRACT_UNIT_ID", MANIFEST_NAME, "Unit identifier is inconsistent with its sources or duplicated")
        units[uid] = unit
        if unit.get("status") not in {"generated", "validated"}:
            add("CONTRACT_UNIT_INCOMPLETE", MANIFEST_NAME, f"Unit has not completed: {unit['name']}")
    for item in data["outputs"]:
        relative = item["path"]
        if relative.casefold() in {path.casefold() for path in outputs}:
            add("CONTRACT_DUPLICATE_OUTPUT", relative, "Output path is owned by multiple translation outputs")
        outputs[relative] = item
        unit = units.get(item["unit_id"])
        if (unit is None or relative not in unit["outputs"]
                or not set(item["sources"]).issubset(unit["sources"])):
            add("CONTRACT_OWNERSHIP", relative, "Output owner or source provenance is inconsistent")
        path = project_path(root, relative)
        if not path.is_file():
            add("CONTRACT_MISSING_OUTPUT", relative, "Declared output does not exist")
            continue
        try:
            code = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            add("CONTRACT_OUTPUT_READ", relative, str(exc))
            continue
        if verify_hashes and content_hash(path.read_bytes()) != item["sha256"]:
            add("CONTRACT_OUTPUT_HASH", relative, "Output changed since its manifest was recorded")
        actual_role = output_role(code)
        if actual_role != item.get("role"):
            add("CONTRACT_ROLE", relative, f"Declared role {item.get('role')} differs from {actual_role}")
        if item.get("role") == "page":
            route = item.get("route")
            expected = Path(relative).relative_to("entry/src/main/ets").with_suffix("").as_posix() if relative.startswith("entry/src/main/ets/") else None
            if not route or route != expected:
                add("CONTRACT_ROUTE", relative, "Page route must match the actual file path")
            elif route in routes:
                add("CONTRACT_DUPLICATE_ROUTE", relative, f"Duplicate route: {route}")
            else:
                routes[route] = relative
    for unit in data["units"]:
        owned = [output for output in data["outputs"] if output["unit_id"] == unit["unit_id"]]
        if set(unit["outputs"]) != {output["path"] for output in owned}:
            add("CONTRACT_OWNERSHIP", MANIFEST_NAME, f"Unit output inventory is inconsistent: {unit['name']}")
        if set(unit["sources"]) != {source for output in owned for source in output["sources"]}:
            add("CONTRACT_SOURCE_COVERAGE", MANIFEST_NAME, f"Unit has untranslated sources: {unit['name']}")
    generated_pages = root / "entry/src/main/ets/pages"
    for path in generated_pages.rglob("*.ets"):
        relative = path.relative_to(root).as_posix()
        if relative not in outputs:
            add("CONTRACT_UNTRACKED_OUTPUT", relative, "Generated file is not owned by the translation manifest")
    for page in data["pages"]:
        output = outputs.get(page["output"])
        if (output is None or output["role"] != "page" or output.get("route") != page["route"]
                or output["unit_id"] != page["unit_id"] or page["source"] not in output["sources"]):
            add("CONTRACT_PAGE_PROVENANCE", page["output"], "Page mapping disagrees with actual translation output")
    for activity in data["required_activities"]:
        mapped = [page for page in data["pages"] if page.get("android_activity") == activity]
        if len(mapped) != 1:
            add("CONTRACT_ACTIVITY_MAPPING", MANIFEST_NAME, f"Activity {activity} must map to exactly one page, got {len(mapped)}")
        elif routes.get(mapped[0].get("route")) != mapped[0].get("output"):
            add("CONTRACT_MISSING_PAGE", str(mapped[0].get("output", "")), f"No runnable page for {activity}")
    launcher = data.get("launcher")
    if not isinstance(launcher, dict) or not launcher.get("route"):
        add("CONTRACT_LAUNCHER", MANIFEST_NAME, "Exactly one Android launcher must map to a generated page")
    elif routes.get(launcher["route"]) != launcher.get("output"):
        add("CONTRACT_LAUNCHER", str(launcher.get("output", "")), "Launcher route does not resolve to its declared page")
    elif len([page for page in data["pages"] if page["android_activity"] == launcher["android_activity"]
              and page["route"] == launcher["route"] and page["output"] == launcher["output"]]) != 1:
        add("CONTRACT_LAUNCHER", launcher["output"], "Launcher source activity does not match its page mapping")
    for relative in outputs:
        path = project_path(root, relative)
        if not path.is_file():
            continue
        try:
            code = code_without_comments(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            continue
        for route in re.findall(r"\b(?:pushUrl|replaceUrl)\s*\(\s*\{[^}]*?\burl\s*:\s*['\"]([^'\"]+)['\"]", code, re.S):
            if route not in routes:
                add("CONTRACT_NAVIGATION", relative, f"Navigation targets an unregistered page: {route}")
    # Generated source trees need no Ability; packaged projects do.
    if (root / "entry/src/main/module.json5").exists():
        profile = root / "entry/src/main/resources/base/profile/main_pages.json"
        ability = root / "entry/src/main/ets/entryability/EntryAbility.ets"
        try:
            registered = json.loads(profile.read_text(encoding="utf-8"))["src"]
            if not isinstance(registered, list) or set(registered) != set(routes):
                add("CONTRACT_REGISTRATION", str(profile.relative_to(root)), "Registered routes differ from declared page outputs")
        except (OSError, ValueError, KeyError, TypeError):
            add("CONTRACT_REGISTRATION", str(profile.relative_to(root)), "Page registration is missing or invalid")
        try:
            contents = code_without_comments(ability.read_text(encoding="utf-8"))
            loads = re.findall(r"\bloadContent\s*\(\s*['\"]([^'\"]+)['\"]", contents)
            if not isinstance(launcher, dict) or loads != [launcher.get("route")]:
                add("CONTRACT_ENTRY", str(ability.relative_to(root)), "Ability must load the declared launcher route exactly once")
        except OSError:
            add("CONTRACT_ENTRY", str(ability.relative_to(root)), "EntryAbility is missing")
    return issues


def build_translation_manifest(source_root: str | Path, units: list, results: list,
                               summaries: dict, android_manifest: dict) -> dict:
    """Use observed planner provenance and files; never infer a target filename."""
    data = {"schema_version": SCHEMA_VERSION, "status": "generated", "source_root": str(Path(source_root).resolve()),
            "units": [], "outputs": [], "pages": [], "required_activities": [], "launcher": None, "issues": []}
    for unit in units:
        uid = stable_unit_id(unit.sources)
        owned = [result for result in results if result.unit_name == unit.name]
        complete = bool(owned) and all(result.success for result in owned)
        data["units"].append({"unit_id": uid, "name": unit.name, "sources": unit.sources,
                              "status": "generated" if complete else "failed", "outputs": []})
        if not complete:
            data["status"] = "incomplete"
        for result in owned:
            if not result.success:
                continue
            path = "entry/src/main/ets/pages/" + result.file_name.replace("\\", "/")
            role = output_role(result.code)
            output = {"path": path, "unit_id": uid, "unit_name": unit.name,
                      "sources": result.sources, "role": role,
                      "route": path.removeprefix("entry/src/main/ets/")[:-4] if role == "page" else None,
                      "sha256": content_hash(result.code)}
            data["outputs"].append(output)
            data["units"][-1]["outputs"].append(path)
    source_classes = {}
    for source, summary in summaries.items():
        class_name = getattr(summary, "class_name", "")
        package = getattr(summary, "package", "")
        if class_name:
            source_classes[(package + "." if package else "") + class_name] = source
    for activity in android_manifest.get("activities", []):
        name = activity["name"]
        data["required_activities"].append(name)
        source = source_classes.get(name)
        if not source:
            data["issues"].append({"code": "CONTRACT_UNSUPPORTED_ACTIVITY", "file": "AndroidManifest.xml", "message": f"Activity has no translated source: {name}"})
            continue
        candidates = [out for out in data["outputs"] if out["role"] == "page" and source in out["sources"]]
        for out in candidates:
            data["pages"].append({"android_activity": name, "source": source, "output": out["path"], "route": out["route"], "unit_id": out["unit_id"]})
    launchers = android_manifest.get("launchers", [])
    if len(launchers) == 1:
        candidate = launchers[0]
        pages = [page for page in data["pages"] if page["android_activity"] == candidate["activity"]]
        if len(pages) == 1:
            data["launcher"] = {"android_activity": candidate["activity"], "component": candidate["component"],
                                "route": pages[0]["route"], "output": pages[0]["output"]}
    if android_manifest.get("error"):
        data["issues"].append({"code": "CONTRACT_ANDROID_MANIFEST", "file": "AndroidManifest.xml", "message": android_manifest["error"]})
    return data

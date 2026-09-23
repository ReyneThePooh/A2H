"""Versioned planning snapshots; resume restores facts without model calls."""
from __future__ import annotations

from dataclasses import asdict, fields, is_dataclass
import json
from pathlib import Path
import types
from typing import Union, get_args, get_origin, get_type_hints

from analyzers.static import FileSummary, XmlSummary
from pipeline.order_determiner import Unit
from run_control import ResumeMismatch, atomic_write_json, fingerprint


SCHEMA_VERSION = 1
_SUMMARY_TYPES = {"java": FileSummary, "xml": XmlSummary}


def _restore(value, expected, location):
    """Strictly rebuild dataclasses instead of retaining nested plain dicts."""
    origin = get_origin(expected)
    if origin in (Union, types.UnionType):
        for alternative in get_args(expected):
            try:
                return _restore(value, alternative, location)
            except ResumeMismatch:
                continue
        raise ResumeMismatch(f"Planning field has incompatible type: {location}")
    if expected is type(None):
        if value is None:
            return None
    elif origin is list:
        if isinstance(value, list):
            return [_restore(item, get_args(expected)[0], f"{location}[{index}]")
                    for index, item in enumerate(value)]
    elif origin is dict:
        if isinstance(value, dict):
            key_type, value_type = get_args(expected)
            return {_restore(key, key_type, location): _restore(item, value_type, f"{location}.{key}")
                    for key, item in value.items()}
    elif is_dataclass(expected):
        if isinstance(value, dict):
            model_fields = {field.name for field in fields(expected)}
            if set(value) != model_fields:
                raise ResumeMismatch(f"Planning dataclass schema changed: {location}")
            hints = get_type_hints(expected)
            return expected(**{key: _restore(item, hints[key], f"{location}.{key}")
                               for key, item in value.items()})
    elif expected in (str, int, bool, float):
        if type(value) is expected:
            return value
    raise ResumeMismatch(f"Planning field has incompatible type: {location}")


def _restore_payload(payload):
    if not isinstance(payload, dict) or set(payload) != {"layers", "deps", "summaries"}:
        raise ResumeMismatch("Planning checkpoint payload schema is invalid")
    layers = _restore(payload["layers"], list[list[Unit]], "layers")
    if not layers or any(not layer for layer in layers):
        raise ResumeMismatch("Planning checkpoint has no nonempty translation layers")
    names = [unit.name for layer in layers for unit in layer]
    if len(names) != len(set(names)) or any(not name for name in names):
        raise ResumeMismatch("Planning checkpoint has duplicate or empty unit names")
    deps_data = _restore(payload["deps"], dict[str, list[str]], "deps")
    names_set = set(names)
    if any(key not in names_set or not set(values).issubset(names_set)
           or len(values) != len(set(values)) for key, values in deps_data.items()):
        raise ResumeMismatch("Planning dependencies reference unknown or duplicate units")
    deps = {name: set(values) for name, values in deps_data.items()}
    if not isinstance(payload["summaries"], dict):
        raise ResumeMismatch("Planning summaries must be an object")
    summaries = {}
    for path, entry in payload["summaries"].items():
        if (not isinstance(path, str) or not path or not isinstance(entry, dict)
                or set(entry) != {"type", "data"} or entry["type"] not in _SUMMARY_TYPES):
            raise ResumeMismatch("Planning summary schema is invalid")
        summary = _restore(entry["data"], _SUMMARY_TYPES[entry["type"]], f"summaries.{path}")
        if summary.file_path != path:
            raise ResumeMismatch("Planning summary path differs from its key")
        summaries[path] = summary
    for layer in layers:
        for unit in layer:
            if not unit.sources or any(source not in summaries for source in unit.sources):
                raise ResumeMismatch(f"Planning unit has no matching source summary: {unit.name}")
    return layers, deps, summaries


def save_planning(path, layers, deps, summaries):
    """Atomically store a complete plan and summaries under a content hash."""
    try:
        payload = {
            "layers": [[asdict(unit) for unit in layer] for layer in layers],
            "deps": {name: sorted(values) for name, values in deps.items()},
            "summaries": {},
        }
        for name, summary in summaries.items():
            if type(summary) is FileSummary:
                kind = "java"
            elif type(summary) is XmlSummary:
                kind = "xml"
            else:
                raise ResumeMismatch(f"Unsupported planning summary type: {type(summary).__name__}")
            payload["summaries"][name] = {"type": kind, "data": asdict(summary)}
        _restore_payload(payload)
        atomic_write_json(Path(path), {"schema_version": SCHEMA_VERSION,
                                      "content_sha256": fingerprint(payload), "payload": payload})
    except ResumeMismatch:
        raise
    except (TypeError, ValueError, AttributeError) as exc:
        raise ResumeMismatch("Cannot serialize planning checkpoint") from exc


def load_planning(path):
    """Restore the exact saved plan; corrupted state is never a cache miss."""
    try:
        saved = json.loads(Path(path).read_text(encoding="utf-8"))
        if (not isinstance(saved, dict)
                or set(saved) != {"schema_version", "content_sha256", "payload"}
                or type(saved["schema_version"]) is not int
                or saved["schema_version"] != SCHEMA_VERSION):
            raise ResumeMismatch("Unsupported planning checkpoint version or envelope")
        if not isinstance(saved["content_sha256"], str) or fingerprint(saved["payload"]) != saved["content_sha256"]:
            raise ResumeMismatch("Planning checkpoint content hash does not match")
        return _restore_payload(saved["payload"])
    except ResumeMismatch:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ResumeMismatch(f"Planning checkpoint is missing or unreadable: {Path(path)}") from exc

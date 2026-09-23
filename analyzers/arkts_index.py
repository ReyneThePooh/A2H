"""Deterministic, explainable indexing for the small ArkTS subset used by ArkUI.

ArkTS components are not TypeScript in the strict sense (``struct`` and the
decorator/component syntax are extensions), so this module deliberately uses a
small lexer plus regular expressions instead of pretending that a generic
TypeScript parser can understand them.  The output is an index of facts that
are useful to the replay gate: source locations, behaviour ids, resources,
event handlers, and statically visible navigation targets.

The parser is intentionally conservative.  It never executes source code and
it only reads files whose resolved path remains below ``project_root``.  A
malformed source file therefore produces a partial index rather than an
unbounded or non-deterministic walk.
"""

from __future__ import annotations

import json
import os
import re
import difflib
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence


# ---------------------------------------------------------------------------
# Serializable data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArkTSReference:
    """A source value and its one-based location."""

    value: str
    file: str
    line: int
    column: int = 1
    context: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ArkTSReference":
        return cls(
            value=str(data.get("value", "")),
            file=str(data.get("file", "")),
            line=int(data.get("line", 0)),
            column=int(data.get("column", 1)),
            context=str(data.get("context", "")),
        )


@dataclass(frozen=True)
class ArkTSMethod:
    name: str
    start_line: int
    end_line: int
    kind: str = "method"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ArkTSMethod":
        return cls(
            name=str(data.get("name", "")),
            start_line=int(data.get("start_line", 0)),
            end_line=int(data.get("end_line", data.get("start_line", 0))),
            kind=str(data.get("kind", "method")),
        )


@dataclass
class ArkTSStruct:
    """An ArkUI ``struct`` and the static facts found inside its body."""

    name: str
    file: str
    start_line: int
    end_line: int
    entry: bool = False
    component: bool = False
    build_start_line: int = 0
    build_end_line: int = 0
    methods: list[ArkTSMethod] = field(default_factory=list)
    ids: list[str] = field(default_factory=list)
    id_refs: list[ArkTSReference] = field(default_factory=list)
    text_literals: list[str] = field(default_factory=list)
    text_refs: list[ArkTSReference] = field(default_factory=list)
    resource_refs: list[str] = field(default_factory=list)
    resource_locations: list[ArkTSReference] = field(default_factory=list)
    event_handlers: list[str] = field(default_factory=list)
    event_refs: list[ArkTSReference] = field(default_factory=list)
    router_targets: list[str] = field(default_factory=list)
    router_refs: list[ArkTSReference] = field(default_factory=list)

    # Compatibility aliases make the index convenient for callers that call a
    # source file's path ``path`` or expect ``is_entry`` from older prototypes.
    @property
    def path(self) -> str:
        return self.file

    @property
    def is_entry(self) -> bool:
        return self.entry

    @property
    def is_component(self) -> bool:
        return self.component

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "file": self.file,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "entry": self.entry,
            "component": self.component,
            "build_start_line": self.build_start_line,
            "build_end_line": self.build_end_line,
            "methods": [method.to_dict() for method in self.methods],
            "ids": list(self.ids),
            "id_refs": [ref.to_dict() for ref in self.id_refs],
            "text_literals": list(self.text_literals),
            "text_refs": [ref.to_dict() for ref in self.text_refs],
            "resource_refs": list(self.resource_refs),
            "resource_locations": [ref.to_dict() for ref in self.resource_locations],
            "event_handlers": list(self.event_handlers),
            "event_refs": [ref.to_dict() for ref in self.event_refs],
            "router_targets": list(self.router_targets),
            "router_refs": [ref.to_dict() for ref in self.router_refs],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ArkTSStruct":
        return cls(
            name=str(data.get("name", "")),
            file=str(data.get("file", data.get("path", ""))),
            start_line=int(data.get("start_line", 0)),
            end_line=int(data.get("end_line", data.get("start_line", 0))),
            entry=bool(data.get("entry", data.get("is_entry", False))),
            component=bool(data.get("component", data.get("is_component", False))),
            build_start_line=int(data.get("build_start_line", 0)),
            build_end_line=int(data.get("build_end_line", 0)),
            methods=[ArkTSMethod.from_dict(item) for item in data.get("methods", [])],
            ids=[str(item) for item in data.get("ids", [])],
            id_refs=[ArkTSReference.from_dict(item) for item in data.get("id_refs", [])],
            text_literals=[str(item) for item in data.get("text_literals", [])],
            text_refs=[ArkTSReference.from_dict(item) for item in data.get("text_refs", [])],
            resource_refs=[str(item) for item in data.get("resource_refs", [])],
            resource_locations=[
                ArkTSReference.from_dict(item)
                for item in data.get("resource_locations", [])
            ],
            event_handlers=[str(item) for item in data.get("event_handlers", [])],
            event_refs=[ArkTSReference.from_dict(item) for item in data.get("event_refs", [])],
            router_targets=[str(item) for item in data.get("router_targets", [])],
            router_refs=[ArkTSReference.from_dict(item) for item in data.get("router_refs", [])],
        )


@dataclass
class ArkTSFile:
    path: str
    line_count: int
    structs: list[ArkTSStruct] = field(default_factory=list)

    @property
    def file(self) -> str:
        return self.path

    @property
    def entry_structs(self) -> list[ArkTSStruct]:
        return [struct for struct in self.structs if struct.entry]

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "line_count": self.line_count,
            "structs": [struct.to_dict() for struct in self.structs],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ArkTSFile":
        return cls(
            path=str(data.get("path", data.get("file", ""))),
            line_count=int(data.get("line_count", 0)),
            structs=[ArkTSStruct.from_dict(item) for item in data.get("structs", [])],
        )


@dataclass
class ArkTSIndex:
    project_root: str
    files: list[ArkTSFile] = field(default_factory=list)
    registered_routes: list[str] = field(default_factory=list)
    route_file: str = ""
    route_files: dict[str, str] = field(default_factory=dict)
    issues: list[dict[str, Any]] = field(default_factory=list)

    @property
    def routes(self) -> list[str]:
        """Alias used by callers that refer to main_pages entries as routes."""
        return self.registered_routes

    @property
    def structs(self) -> list[ArkTSStruct]:
        return [struct for file in self.files for struct in file.structs]

    @property
    def pages(self) -> list[ArkTSStruct]:
        return [struct for struct in self.structs if struct.entry]

    @property
    def all_ids(self) -> list[str]:
        return sorted({item for struct in self.structs for item in struct.ids})

    def structs_for_route(self, route: str) -> list[ArkTSStruct]:
        normalized = normalize_route(route)
        relative = self.route_files.get(normalized)
        if relative is None:
            matches = _route_file_matches(
                normalized, (file.path for file in self.files)
            )
            relative = matches[0] if matches else _route_to_relative_ets(normalized)
        return [struct for file in self.files if file.path == relative for struct in file.structs]

    def repair_regions(
        self,
        file: str,
        identifiers: Iterable[str] = (),
        route: str = "",
    ) -> list[dict[str, Any]]:
        """Return the smallest indexed source regions relevant to a failure.

        A target id resolves to the containing method when possible, then to
        the component's ``build`` body.  A route without an id resolves to the
        page component.  Empty results mean the failure cannot be localized
        statically and callers should keep the existing file-level policy.
        """
        normalized_file = str(file).replace("\\", "/")
        structs = [item for entry in self.files if entry.path == normalized_file
                   for item in entry.structs]
        if route:
            routed = self.structs_for_route(route)
            if routed:
                structs = routed
        wanted = {str(value) for value in identifiers if str(value)}
        regions: list[dict[str, Any]] = []
        for struct in structs:
            selected = [ref for ref in struct.id_refs if ref.value in wanted]
            if selected:
                for ref in selected:
                    method = next((item for item in struct.methods
                                   if item.start_line <= ref.line <= item.end_line), None)
                    start = method.start_line if method else (struct.build_start_line or struct.start_line)
                    end = method.end_line if method else (struct.build_end_line or struct.end_line)
                    regions.append({"start_line": start, "end_line": end,
                                    "reason": f"id:{ref.value}", "struct": struct.name})
            elif not wanted:
                start = struct.build_start_line or struct.start_line
                end = struct.build_end_line or struct.end_line
                regions.append({"start_line": start, "end_line": end,
                                "reason": "page" if struct.entry else "component",
                                "struct": struct.name})
        unique = {(int(item["start_line"]), int(item["end_line"]), item["reason"]): item
                  for item in regions if int(item["start_line"]) > 0}
        return [unique[key] for key in sorted(unique)]

    def repair_context(self, file: str, identifiers: Iterable[str] = (), route: str = "") -> str:
        regions = self.repair_regions(file, identifiers, route)
        if not regions:
            return "静态索引未能把失败定位到具体组件或方法；保持文件级证据约束。"
        return "静态索引定位范围：" + ", ".join(
            f"{item['struct']} L{item['start_line']}-L{item['end_line']} ({item['reason']})"
            for item in regions
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "project_root": self.project_root,
            "files": [item.to_dict() for item in self.files],
            "registered_routes": list(self.registered_routes),
            "route_file": self.route_file,
            "route_files": {key: self.route_files[key] for key in sorted(self.route_files)},
            "issues": [dict(item) for item in self.issues],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ArkTSIndex":
        return cls(
            project_root=str(data.get("project_root", "")),
            files=[ArkTSFile.from_dict(item) for item in data.get("files", [])],
            registered_routes=[str(item) for item in data.get("registered_routes", data.get("routes", []))],
            route_file=str(data.get("route_file", "")),
            route_files={str(k): str(v) for k, v in dict(data.get("route_files", {})).items()},
            issues=[dict(item) for item in data.get("issues", [])],
        )

    def save(self, path: str | os.PathLike[str]) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.tmp")
        try:
            temporary.write_text(
                json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "ArkTSIndex":
        with Path(path).open("r", encoding="utf-8") as handle:
            return cls.from_dict(json.load(handle))


def save_arkts_index(index: ArkTSIndex, path: str | os.PathLike[str]) -> None:
    index.save(path)


def load_arkts_index(path: str | os.PathLike[str]) -> ArkTSIndex:
    return ArkTSIndex.load(path)


# ---------------------------------------------------------------------------
# Lexing and source extraction
# ---------------------------------------------------------------------------


_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", ".idea", ".hvigor", "build", "node_modules",
    "oh_modules", "dist", "out", "coverage", "ohosTest", "test", "tests",
})
_STRUCT_RE = re.compile(r"\bstruct\s+(?P<name>[A-Za-z_]\w*)")
_METHOD_RE = re.compile(
    r"\b(?P<name>[A-Za-z_]\w*)\s*\([^;{}\n]*\)\s*(?:\:\s*[^\{\n]+)?\s*\{"
)
_ID_RE = re.compile(r"\.\s*id\s*\(\s*(?P<quote>['\"])(?P<value>.*?)(?P=quote)\s*\)")
_RESOURCE_RE = re.compile(
    r"\$r\s*\(\s*(?P<quote>['\"])(?P<value>app\.[^'\"]+)(?P=quote)\s*\)"
)
_ROUTER_OBJECT_RE = re.compile(
    r"\brouter\s*\.\s*pushUrl\s*\(\s*\{[^{}]*?\burl\s*:\s*"
    r"(?P<quote>['\"])(?P<value>[^'\"]+)(?P=quote)",
    re.DOTALL,
)
_ROUTER_DIRECT_RE = re.compile(
    r"\brouter\s*\.\s*pushUrl\s*\(\s*(?P<quote>['\"])(?P<value>[^'\"]+)(?P=quote)"
)
_EVENT_CALL_RE = re.compile(r"\.\s*(?P<name>on[A-Z][A-Za-z0-9_]*)\s*\(")
_EVENT_PROP_RE = re.compile(r"\b(?P<name>on(?:Click|Change|Action|Submit|Touch|LongPress|Focus|Blur))\s*:")
_STRING_RE = re.compile(r"(?P<quote>['\"])(?P<value>(?:\\.|(?!\1).)*?)(?P=quote)", re.DOTALL)


def _mask_comments(source: str) -> str:
    """Blank comments while preserving newlines and string contents."""
    chars = list(source)
    i = 0
    state = "normal"
    quote = ""
    escaped = False
    while i < len(chars):
        current = chars[i]
        following = chars[i + 1] if i + 1 < len(chars) else ""
        if state == "normal":
            if current == "/" and following == "/":
                chars[i] = chars[i + 1] = " "
                i += 2
                state = "line"
                continue
            if current == "/" and following == "*":
                chars[i] = chars[i + 1] = " "
                i += 2
                state = "block"
                continue
            if current in "'\"`":
                state = "string"
                quote = current
                escaped = False
            i += 1
            continue
        if state == "line":
            if current == "\n":
                state = "normal"
            elif current != "\r":
                chars[i] = " "
            i += 1
            continue
        if state == "block":
            if current == "*" and following == "/":
                chars[i] = chars[i + 1] = " "
                i += 2
                state = "normal"
            else:
                if current not in "\r\n":
                    chars[i] = " "
                i += 1
            continue
        # string
        if escaped:
            escaped = False
        elif current == "\\":
            escaped = True
        elif current == quote:
            state = "normal"
        i += 1
    return "".join(chars)


def _mask_strings(source: str) -> str:
    """Blank string contents while preserving positions and line breaks."""
    chars = list(source)
    i = 0
    quote = ""
    escaped = False
    while i < len(chars):
        current = chars[i]
        if not quote:
            if current in "'\"`":
                quote = current
                escaped = False
                i += 1
            else:
                i += 1
            continue
        if escaped:
            if current not in "\r\n":
                chars[i] = " "
            escaped = False
            i += 1
            continue
        if current == "\\":
            chars[i] = " "
            escaped = True
            i += 1
            continue
        if current == quote:
            quote = ""
            i += 1
            continue
        if current not in "\r\n":
            chars[i] = " "
        i += 1
    return "".join(chars)


def _line_starts(source: str) -> list[int]:
    starts = [0]
    starts.extend(index + 1 for index, value in enumerate(source) if value == "\n")
    return starts


def _line_col(starts: Sequence[int], offset: int) -> tuple[int, int]:
    # Number of lines in an ArkTS file is small compared with a source tree;
    # binary search keeps this deterministic without retaining line objects.
    import bisect

    line_index = bisect.bisect_right(starts, offset) - 1
    return line_index + 1, offset - starts[line_index] + 1


def _unquote(value: str) -> str:
    return (
        value.replace("\\'", "'")
        .replace('\\"', '"')
        .replace("\\\\", "\\")
        .replace("\\n", "\n")
        .replace("\\r", "\r")
    )


def _match_brace_end(source: str, opening: int) -> int:
    depth = 0
    quote = ""
    escaped = False
    for index in range(opening, len(source)):
        value = source[index]
        if quote:
            if escaped:
                escaped = False
            elif value == "\\":
                escaped = True
            elif value == quote:
                quote = ""
            continue
        if value in "'\"`":
            quote = value
            continue
        if value == "{":
            depth += 1
        elif value == "}":
            depth -= 1
            if depth == 0:
                return index
    return len(source)


def _decorator_prefix(source: str, masked: str, struct_start: int) -> str:
    """Return decorators directly attached to a struct declaration."""
    line_start = masked.rfind("\n", 0, struct_start) + 1
    prefix = masked[line_start:struct_start]
    # Decorators can be on preceding lines.  Walk contiguous decorator/export
    # lines; unrelated code terminates the declaration prefix.
    cursor = line_start
    while cursor > 0:
        previous_end = cursor - 1
        previous_start = masked.rfind("\n", 0, previous_end) + 1
        text = masked[previous_start:previous_end + 1].strip()
        if not text:
            cursor = previous_start
            continue
        if re.fullmatch(r"(?:@[A-Za-z_]\w*(?:\s*\([^\n{}]*\))?\s*)+", text):
            prefix = masked[previous_start:struct_start] + prefix
            cursor = previous_start
            continue
        if re.fullmatch(r"export", text):
            prefix = masked[previous_start:struct_start] + prefix
        break
    return prefix


def _nearest_method(methods: Sequence[ArkTSMethod], line: int) -> str:
    candidates = [method.name for method in methods if method.start_line <= line <= method.end_line]
    return candidates[-1] if candidates else ""


def _extract_structs(source: str, relative_path: str) -> list[ArkTSStruct]:
    masked = _mask_comments(source)
    syntax = _mask_strings(masked)
    starts = _line_starts(source)
    structs: list[ArkTSStruct] = []
    for struct_match in _STRUCT_RE.finditer(syntax):
        opening = syntax.find("{", struct_match.end())
        if opening < 0:
            continue
        closing = _match_brace_end(masked, opening)
        start_line, _ = _line_col(starts, struct_match.start())
        end_line, _ = _line_col(starts, max(opening, closing - 1))
        prefix = _decorator_prefix(source, syntax, struct_match.start())
        decorators = set(re.findall(r"@([A-Za-z_]\w*)", prefix))
        body_start = opening + 1
        body = masked[body_start:closing]
        body_syntax = syntax[body_start:closing]
        body_offset = body_start

        methods: list[ArkTSMethod] = []
        for method_match in _METHOD_RE.finditer(body_syntax):
            absolute = body_offset + method_match.start()
            method_line, _ = _line_col(starts, absolute)
            if method_line < start_line or method_line > end_line:
                continue
            name = method_match.group("name")
            # ArkUI's declarative children use the same ``Name() {`` shape as
            # a function call (Column, Text, List...).  Component names start
            # with an upper-case letter, while user methods are conventionally
            # lower-case; excluding the former keeps ``methods`` useful for
            # source navigation without trying to parse the full ArkTS grammar.
            if name in {"if", "for", "while", "switch", "catch", "with"} or name[:1].isupper():
                continue
            method_opening = body_offset + method_match.end() - 1
            method_closing = _match_brace_end(masked, method_opening)
            method_end_line, _ = _line_col(starts, max(method_opening, method_closing - 1))
            methods.append(ArkTSMethod(name, method_line, method_end_line))
        methods.sort(key=lambda item: (item.start_line, item.end_line, item.name))

        id_refs: list[ArkTSReference] = []
        ignored_string_spans: list[tuple[int, int]] = []
        for match in _ID_RE.finditer(body):
            absolute = body_offset + match.start()
            line, column = _line_col(starts, absolute)
            id_refs.append(ArkTSReference(_unquote(match.group("value")), relative_path, line, column, "id"))
            ignored_string_spans.append((match.start(), match.end()))

        resource_locations: list[ArkTSReference] = []
        for match in _RESOURCE_RE.finditer(body):
            absolute = body_offset + match.start()
            line, column = _line_col(starts, absolute)
            resource_locations.append(
                ArkTSReference(match.group("value"), relative_path, line, column, "$r")
            )
            ignored_string_spans.append((match.start(), match.end()))

        event_refs: list[ArkTSReference] = []
        for pattern in (_EVENT_CALL_RE, _EVENT_PROP_RE):
            for match in pattern.finditer(body):
                absolute = body_offset + match.start()
                line, column = _line_col(starts, absolute)
                event_refs.append(
                    ArkTSReference(match.group("name"), relative_path, line, column, "event")
                )
        event_refs.sort(key=lambda item: (item.line, item.column, item.value))

        router_refs: list[ArkTSReference] = []
        for pattern in (_ROUTER_OBJECT_RE, _ROUTER_DIRECT_RE):
            for match in pattern.finditer(body):
                absolute = body_offset + match.start()
                line, column = _line_col(starts, absolute)
                raw_target = _unquote(match.group("value"))
                normalized_target = normalize_route(raw_target)
                router_refs.append(
                    ArkTSReference(
                        normalized_target or raw_target,
                        relative_path,
                        line,
                        column,
                        "router.pushUrl",
                    )
                )
                ignored_string_spans.append((match.start(), match.end()))
        # Object and direct patterns can overlap only when a source is unusual;
        # deduplicate by location and target for stable output.
        router_refs = sorted(
            {(ref.line, ref.column, ref.value): ref for ref in router_refs}.values(),
            key=lambda item: (item.line, item.column, item.value),
        )

        id_values = sorted({ref.value for ref in id_refs})
        resource_values = sorted({ref.value for ref in resource_locations})
        event_values = sorted({ref.value for ref in event_refs})
        router_values = sorted({ref.value for ref in router_refs})
        text_refs: list[ArkTSReference] = []
        for match in _STRING_RE.finditer(body):
            value = _unquote(match.group("value"))
            if not value or any(start <= match.start() < end for start, end in ignored_string_spans):
                continue
            absolute = body_offset + match.start()
            line, column = _line_col(starts, absolute)
            text_refs.append(ArkTSReference(value, relative_path, line, column, "literal"))
        text_refs.sort(key=lambda item: (item.line, item.column, item.value))

        build_methods = [method for method in methods if method.name == "build"]
        build = build_methods[0] if build_methods else None
        structs.append(
            ArkTSStruct(
                name=struct_match.group("name"),
                file=relative_path,
                start_line=start_line,
                end_line=end_line,
                entry="Entry" in decorators,
                component=any(name in decorators for name in ("Component", "ComponentV2")),
                build_start_line=build.start_line if build else 0,
                build_end_line=build.end_line if build else 0,
                methods=methods,
                ids=id_values,
                id_refs=id_refs,
                text_literals=sorted({ref.value for ref in text_refs}),
                text_refs=text_refs,
                resource_refs=resource_values,
                resource_locations=resource_locations,
                event_handlers=event_values,
                event_refs=event_refs,
                router_targets=router_values,
                router_refs=router_refs,
            )
        )
    return sorted(structs, key=lambda item: (item.file, item.start_line, item.name))


# ---------------------------------------------------------------------------
# Project and route indexing
# ---------------------------------------------------------------------------


def _resolved_root(project_root: str | os.PathLike[str]) -> Path:
    root = Path(project_root).expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(f"ArkTS project root does not exist: {project_root}")
    if not root.is_dir():
        raise NotADirectoryError(f"ArkTS project root is not a directory: {project_root}")
    return root


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root)
        return True
    except (OSError, ValueError):
        return False


def _safe_relative(root: Path, candidate: Path) -> str | None:
    if not _inside(root, candidate):
        return None
    try:
        return candidate.resolve().relative_to(root).as_posix()
    except (OSError, ValueError):
        return None


def normalize_route(route: str) -> str:
    """Normalize a route without permitting path traversal."""
    value = str(route or "").strip().replace("\\", "/")
    # Check absolute paths before trimming presentation punctuation.  A route
    # such as ``/pages/Main`` must never become a relative route by parsing.
    if not value or value.startswith("/"):
        return ""
    if ":" in value:
        # Ability:pages/Index is common in page-pair files.  Reject URL,
        # drive-letter and other schemes instead of treating them as routes.
        prefix, suffix = value.split(":", 1)
        if (not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", prefix)
                or not suffix or suffix.startswith("/")):
            return ""
        value = suffix
    value = value.removeprefix("./")
    if value.endswith("/"):
        value = value.rstrip("/")
    if value.endswith(".ets"):
        value = value[:-4]
    parts = PurePosixPath(value).parts
    if not value or value.startswith("/") or ".." in parts or any(part == "" for part in parts):
        return ""
    return "/".join(parts)


def _route_to_relative_ets(route: str) -> str:
    route = normalize_route(route)
    if not route:
        return ""
    return f"entry/src/main/ets/{route}.ets"


def _route_file_matches(route: str, file_paths: Iterable[str]) -> list[str]:
    """Resolve a registered route against standard ArkTS path suffixes.

    Packaged projects normally have ``entry/`` at the index root, while
    template checkouts often add a wrapper directory such as ``template/``.
    Matching the known suffixes keeps both layouts equivalent without
    allowing an arbitrary route to escape the indexed file set.
    """
    normalized = normalize_route(route)
    if not normalized:
        return []
    suffixes = (
        _route_to_relative_ets(normalized),
        f"src/main/ets/{normalized}.ets",
        f"entry/src/ets/{normalized}.ets",
        f"ets/{normalized}.ets",
        f"src/ets/{normalized}.ets",
    )
    matches = {
        path.replace("\\", "/")
        for path in file_paths
        if any(path.replace("\\", "/") == suffix
               or path.replace("\\", "/").endswith("/" + suffix)
               for suffix in suffixes)
    }
    return sorted(matches, key=str.casefold)


def _route_candidates(root: Path) -> list[Path]:
    exact = [
        root / "entry/src/main/resources/base/profile/main_pages.json",
        root / "src/main/resources/base/profile/main_pages.json",
        root / "resources/base/profile/main_pages.json",
    ]
    found: list[Path] = []
    for path in exact:
        if path.is_file() and _inside(root, path):
            found.append(path)
    if found:
        return found
    for path in sorted(root.rglob("main_pages.json"), key=lambda item: item.as_posix().casefold()):
        relative = _safe_relative(root, path)
        if (relative and relative.endswith("resources/base/profile/main_pages.json")
                and not any(part in _SKIP_DIRS for part in Path(relative).parts)):
            found.append(path)
    return found


def _read_routes(root: Path) -> tuple[list[str], str, list[dict[str, Any]]]:
    candidates = _route_candidates(root)
    if not candidates:
        return [], "", []
    route_file = candidates[0]
    issues: list[dict[str, Any]] = []
    try:
        data = json.loads(route_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        relative = _safe_relative(root, route_file) or route_file.name
        return [], relative, [{
            "code": "INVALID_ROUTE_REGISTRY",
            "file": relative,
            "line": 1,
            "message": f"Unable to parse main_pages.json: {exc}",
            "severity": "error",
        }]
    values = data.get("src") if isinstance(data, dict) else None
    if not isinstance(values, list):
        relative = _safe_relative(root, route_file) or route_file.name
        return [], relative, [{
            "code": "INVALID_ROUTE_REGISTRY",
            "file": relative,
            "line": 1,
            "message": "main_pages.json must contain an array field 'src'",
            "severity": "error",
        }]
    routes: list[str] = []
    for index, raw in enumerate(values):
        route = normalize_route(raw) if isinstance(raw, str) else ""
        if not route:
            relative = _safe_relative(root, route_file) or route_file.name
            issues.append({
                "code": "INVALID_ROUTE",
                "file": relative,
                "line": 1,
                "route": str(raw),
                "index": index,
                "message": "Route is empty or escapes the project root",
                "severity": "error",
            })
            continue
        if route not in routes:
            routes.append(route)
    return routes, _safe_relative(root, route_file) or route_file.name, issues


def build_arkts_index(project_root: str | os.PathLike[str]) -> ArkTSIndex:
    """Build a deterministic index of project ``.ets`` files and routes."""
    root = _resolved_root(project_root)
    files: list[ArkTSFile] = []
    candidates: list[tuple[str, Path]] = []
    for path in root.rglob("*.ets"):
        # Apply ignore-directory policy relative to the project root.  Using
        # ``path.parts`` here would also inspect parent directories, so a
        # perfectly valid checkout rooted at ``.../tests/app`` was skipped in
        # its entirety.
        try:
            relative_path = path.resolve().relative_to(root)
        except (OSError, ValueError):
            continue
        if any(part in _SKIP_DIRS for part in relative_path.parts):
            continue
        relative = _safe_relative(root, path)
        if relative is None or not path.is_file():
            continue
        candidates.append((relative, path))
    for relative, path in sorted(candidates, key=lambda item: item[0].casefold()):
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            files.append(ArkTSFile(relative, 0, []))
            continue
        files.append(ArkTSFile(relative, source.count("\n") + 1, _extract_structs(source, relative)))

    routes, route_file, issues = _read_routes(root)
    route_files: dict[str, str] = {}
    file_paths = {file.path for file in files}
    for route in routes:
        matches = _route_file_matches(route, file_paths)
        if len(matches) == 1:
            route_files[route] = matches[0]
        elif len(matches) > 1:
            issues.append({
                "code": "AMBIGUOUS_ROUTE_FILE",
                "file": route_file,
                "line": 1,
                "route": route,
                "candidates": matches,
                "message": f"Registered route resolves to multiple .ets sources: {route}",
                "severity": "error",
            })
        else:
            issues.append({
                "code": "MISSING_ROUTE_FILE",
                "file": route_file,
                "line": 1,
                "route": route,
                "message": f"Registered route has no matching .ets source: {route}",
                "severity": "error",
            })
    return ArkTSIndex(
        project_root=str(root),
        files=files,
        registered_routes=routes,
        route_file=route_file,
        route_files={key: route_files[key] for key in sorted(route_files)},
        issues=sorted(issues, key=lambda item: (str(item.get("file", "")), int(item.get("line", 0)), str(item.get("code", "")))),
    )


# ---------------------------------------------------------------------------
# Static contract validation
# ---------------------------------------------------------------------------


def _issue(code: str, message: str, **fields: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "code": code,
        "message": message,
        "severity": "error",
    }
    result.update({key: value for key, value in fields.items() if value is not None and value != ""})
    return result


def _mapping_from_page_pairs(page_pairs: Any) -> dict[str, str]:
    if page_pairs is None:
        return {}
    if isinstance(page_pairs, Mapping):
        if isinstance(page_pairs.get("pairs"), list):
            return {str(pair[0]): str(pair[1]) for pair in page_pairs["pairs"] if isinstance(pair, (list, tuple)) and len(pair) >= 2}
        return {str(key): str(value) for key, value in page_pairs.items()}
    mapping = getattr(page_pairs, "mapping", None)
    if isinstance(mapping, Mapping):
        return {str(key): str(value) for key, value in mapping.items()}
    return {}


def _trace_value(trace: Any, key: str, default: Any = None) -> Any:
    if isinstance(trace, Mapping):
        return trace.get(key, default)
    return getattr(trace, key, default)


def _event_value(event: Any, key: str, default: Any = None) -> Any:
    if isinstance(event, Mapping):
        return event.get(key, default)
    return getattr(event, key, default)


def _state_page(state: Any) -> str:
    return str(_trace_value(state, "page", "") or "")


def _target_id(event: Any) -> str:
    target = _event_value(event, "target")
    if target is None:
        return ""
    if isinstance(target, Mapping):
        return str(target.get("id_hint", "") or "")
    return str(getattr(target, "id_hint", "") or "")


def _harmony_route(page: str, mapping: Mapping[str, str], registered: Sequence[str]) -> str:
    mapped = mapping.get(page, page)
    normalized = normalize_route(mapped)
    if normalized in registered:
        return normalized
    # A page pair may contain an Ability prefix or a bare page class.  Use a
    # deterministic stem fallback only when it identifies exactly one route.
    stem = re.sub(r"(?:activity|ability|page|view)$", "", page.rsplit(".", 1)[-1].casefold())
    candidates = [route for route in registered if re.sub(r"(?:activity|ability|page|view)$", "", route.rsplit("/", 1)[-1].casefold()) == stem]
    return candidates[0] if len(candidates) == 1 else normalized


def validate_static_contract(
    index: ArkTSIndex,
    page_pairs: Any = None,
    traces: Iterable[Any] | None = None,
) -> list[dict[str, Any]]:
    """Validate replay-facing ids and navigation against the static index.

    ``traces`` accepts schema dataclasses or their JSON dictionaries.  Missing
    ids are reported against the route inferred from each event's pre-state;
    route targets are checked for every indexed struct.  Output is sorted by
    trace/step/file/line/code so CI and repair reports remain reproducible.
    """
    issues: list[dict[str, Any]] = [dict(item) for item in index.issues]
    registered = set(index.registered_routes)
    for struct in index.structs:
        for ref in struct.router_refs:
            target = normalize_route(ref.value)
            if not target:
                issues.append(_issue(
                    "INVALID_ROUTE",
                    f"router.pushUrl contains an unsafe route: {ref.value}",
                    file=ref.file,
                    line=ref.line,
                    column=ref.column,
                    route=ref.value,
                    struct=struct.name,
                ))
            elif target not in registered:
                issues.append(_issue(
                    "UNREGISTERED_ROUTE",
                    f"router.pushUrl targets an unregistered route: {target}",
                    file=ref.file,
                    line=ref.line,
                    column=ref.column,
                    route=target,
                    struct=struct.name,
                ))

    mapping = _mapping_from_page_pairs(page_pairs)
    all_traces: list[Any] = list(traces or [])
    for trace in all_traces:
        trace_id = str(_trace_value(trace, "trace_id", "") or "")
        initial = _trace_value(trace, "initial_state")
        events = _trace_value(trace, "events", []) or []
        schema_version = _trace_value(trace, "schema_version")
        # Static contracts must not mask the gate's authoritative baseline
        # diagnosis.  A legacy or incomplete trace has no reliable page
        # ownership for its target ids; let Trace.baseline_errors() report it
        # as BASELINE_INVALID instead.
        if (schema_version is not None and schema_version != 2) or initial is None:
            continue
        if schema_version == 2 and any(
                _event_value(event, "pre_state") is None
                or _event_value(event, "post_state") is None
                for event in events):
            continue
        current_page = _state_page(initial)
        for event in events:
            pre_state = _event_value(event, "pre_state")
            page = _state_page(pre_state) or current_page
            if page:
                current_page = page
            target_id = _target_id(event)
            if target_id:
                route = _harmony_route(page, mapping, index.registered_routes)
                structs = index.structs_for_route(route) if route else []
                if not structs:
                    # If a route is not registered, searching globally would
                    # hide a page ownership error; retain the explicit route
                    # in the issue instead.
                    structs = []
                if not any(target_id in struct.ids for struct in structs):
                    step = _event_value(event, "step")
                    fields: dict[str, Any] = {
                        "trace_id": trace_id,
                        "step": int(step) if isinstance(step, int) or str(step).isdigit() else step,
                        "id_hint": target_id,
                        "page": page,
                    }
                    if route:
                        fields["route"] = route
                    issues.append(_issue(
                        "MISSING_ID",
                        f"Replay target id is not indexed on page {route or page or '<unknown>'}: {target_id}",
                        **fields,
                    ))
            post_state = _event_value(event, "post_state")
            post_page = _state_page(post_state)
            if post_page:
                current_page = post_page

    def sort_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
        step = item.get("step", 0)
        try:
            step_value = int(step)
        except (TypeError, ValueError):
            step_value = 0
        return (
            str(item.get("trace_id", "")),
            step_value,
            str(item.get("file", "")),
            int(item.get("line", 0) or 0),
            str(item.get("code", "")),
            str(item.get("message", "")),
        )

    return sorted(issues, key=sort_key)


def changed_line_ranges(original: str, updated: str) -> list[dict[str, tuple[int, int]]]:
    """Return changed old/new line ranges for a patch scope check."""
    old_lines, new_lines = original.splitlines(), updated.splitlines()
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    changed: list[dict[str, tuple[int, int]]] = []
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        changed.append({
            "old": (old_start + 1, max(old_end, old_start + 1)),
            "new": (new_start + 1, max(new_end, new_start + 1)),
        })
    return changed


def validate_patch_scope(
    original: str,
    updated: str,
    allowed_regions: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Reject edits whose old and new line ranges leave indexed regions."""
    regions = [
        (int(item.get("start_line", 0)), int(item.get("end_line", 0)))
        for item in allowed_regions
        if int(item.get("start_line", 0)) > 0
    ]
    if not regions:
        return []
    issues = []
    for change in changed_line_ranges(original, updated):
        violations = {}
        for side, (start, end) in change.items():
            if not any(region_start <= start and end <= region_end
                       for region_start, region_end in regions):
                violations[side] = {"start_line": start, "end_line": end}
        if violations:
            issues.append({"code": "PATCH_SCOPE_OUTSIDE_INDEX",
                           "message": "patch changes lines outside indexed repair region",
                           "ranges": violations})
    return issues


__all__ = [
    "ArkTSFile",
    "ArkTSIndex",
    "ArkTSMethod",
    "ArkTSReference",
    "ArkTSStruct",
    "build_arkts_index",
    "changed_line_ranges",
    "load_arkts_index",
    "normalize_route",
    "save_arkts_index",
    "validate_patch_scope",
    "validate_static_contract",
]

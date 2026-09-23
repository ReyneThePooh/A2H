"""Build and optionally validate the deterministic ArkTS static index.

Examples:
    python tools/index_arkts.py path/to/harmony --output gate/arkts_index.json
    python tools/index_arkts.py path/to/harmony --seeds gate/seeds --check
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# ``python tools/index_arkts.py`` places only ``tools/`` on sys.path.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyzers.arkts_index import (
    build_arkts_index,
    save_arkts_index,
    validate_static_contract,
)


def _load_json(path: Path, default):
    if not path.is_file():
        return default
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return default
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path, help="Harmony project root")
    parser.add_argument("--output", type=Path, help="index JSON output path")
    parser.add_argument("--seeds", type=Path, help="directory containing replay seed JSON")
    parser.add_argument("--page-pairs", type=Path, help="page_pairs.json")
    parser.add_argument("--check", action="store_true", help="validate replay-facing static contracts")
    args = parser.parse_args(argv)

    index = build_arkts_index(args.project)
    if args.output:
        save_arkts_index(index, args.output)
    else:
        print(json.dumps(index.to_dict(), ensure_ascii=False, indent=2))

    if not args.check:
        return 0

    seeds = []
    if args.seeds and args.seeds.is_dir():
        for path in sorted(args.seeds.glob("*.json"), key=lambda item: item.name.casefold()):
            payload = _load_json(path, None)
            if isinstance(payload, dict):
                seeds.append(payload)
    pairs = _load_json(args.page_pairs, {}) if args.page_pairs else {}
    issues = validate_static_contract(index, pairs, seeds)
    print(json.dumps({"issues": issues}, ensure_ascii=False, indent=2))
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())

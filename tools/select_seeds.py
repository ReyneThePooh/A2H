# -*- coding: utf-8 -*-
"""从 fuzz 录制结果中挑选种子轨迹，复制到差分测试种子目录。

策略：
1. 过滤掉步数 < min_steps、超过 max_steps 或含崩溃步的轨迹；
2. 贪心选择：每次挑"新覆盖页面数最多"的轨迹；新覆盖相同则优先较短轨迹；
3. 若名额未满，补充未选轨迹（优先含 NewBoardActivity 的创建流，同样优先较短）；
4. 复制为 seed_NN_<页面简述>.json，并写入 app_pkg_harmony。

用法：
  python tools/select_seeds.py --src record_out/traces --dst .diff_gate/seeds \
      --bundle com.example.myapplication --max 8 --max-steps 8
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

# Running ``python tools/select_seeds.py`` puts only ``tools/`` on
# ``sys.path``.  Add the repository root explicitly so the selector can use
# the canonical Trace v2 validator instead of maintaining a second schema
# implementation.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diff_tester.schemas import Trace


def load(fp: Path) -> dict:
    return json.loads(fp.read_text(encoding="utf-8"))


def pages_of(trace: dict) -> list[str]:
    seen: list[str] = []
    for ev in trace.get("events", []):
        ps = ev.get("post_state") or {}
        page = (ps.get("page") or "").strip()
        if page and page not in seen:
            seen.append(page)
    return seen


def has_crash(trace: dict) -> bool:
    return any((ev.get("post_state") or {}).get("crash_sig")
               for ev in trace.get("events", []))


def baseline_errors(trace: dict) -> list[str]:
    """Return the canonical Trace v2 baseline errors for a seed payload.

    Seed selection must not turn a legacy recording into a gate input.  The
    gate remains the final authority, but rejecting invalid evidence here
    gives the user a useful filename and preserves the rule that v1 traces
    are re-recorded rather than patched in place.
    """
    try:
        return Trace.from_dict(trace).baseline_errors()
    except (KeyError, TypeError, ValueError) as exc:
        return [f"invalid trace payload: {exc}"]


def short(page: str) -> str:
    name = page.rsplit(".", 1)[-1]
    return name.removesuffix("Activity") or name


def select_candidates(candidates: list[dict], max_count: int) -> list[dict]:
    """Select a deterministic, page-covering short-seed subset.

    Candidate dictionaries contain ``pages`` and ``n`` fields.  Page gain is
    the primary objective; event count is the tie-breaker so a shorter trace
    wins when it covers the same number of new pages.  The final filename
    tie-breaker keeps repeated runs stable even when recording order changes.
    """
    if type(max_count) is not int or max_count < 0:
        raise ValueError("max_count must be a non-negative integer")
    if max_count == 0 or not candidates:
        return []

    all_pages = set().union(*(set(c["pages"]) for c in candidates))
    covered: set[str] = set()
    chosen: list[dict] = []
    pool = list(candidates)
    while pool and covered != all_pages and len(chosen) < max_count:
        pool.sort(key=lambda c: (
            -len(set(c["pages"]) - covered),
            c["n"],
            c["fp"].name,
        ))
        best = pool.pop(0)
        gain = set(best["pages"]) - covered
        if not gain and covered:
            break
        chosen.append(best)
        covered |= set(best["pages"])

    # Fill remaining slots while retaining the creation-flow preference.  A
    # shorter trace wins within the same preference class.
    rest = [c for c in candidates if c not in chosen]
    rest.sort(key=lambda c: (
        -int("NewBoard" in "".join(c["pages"])),
        c["n"],
        c["fp"].name,
    ))
    chosen.extend(rest[:max(0, max_count - len(chosen))])
    return chosen


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="record_out/traces")
    ap.add_argument("--dst", default=".diff_gate/seeds")
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--max", type=int, default=8)
    ap.add_argument("--min-steps", type=int, default=4)
    ap.add_argument("--max-steps", type=int, default=None,
                    help="可选：排除超过该步数的轨迹")
    args = ap.parse_args()

    if args.max < 0 or args.min_steps < 0 \
            or (args.max_steps is not None and args.max_steps < 0):
        ap.error("--max、--min-steps、--max-steps 必须为非负整数")
    if (args.max_steps is not None
            and args.max_steps < args.min_steps):
        ap.error("--max-steps 不能小于 --min-steps")

    src, dst = Path(args.src), Path(args.dst)
    candidates = []
    for fp in sorted(src.glob("*.json")):
        t = load(fp)
        errors = baseline_errors(t)
        if errors:
            preview = "; ".join(errors[:2])
            print(f"  跳过 {fp.name}: 基线无效（{preview}）")
            continue
        n = len(t.get("events", []))
        pgs = pages_of(t)
        if n < args.min_steps:
            print(f"  跳过 {fp.name}: 仅 {n} 步")
            continue
        if args.max_steps is not None and n > args.max_steps:
            print(f"  跳过 {fp.name}: {n} 步超过上限 {args.max_steps}")
            continue
        if has_crash(t):
            print(f"  跳过 {fp.name}: 含崩溃步")
            continue
        candidates.append({"fp": fp, "trace": t, "n": n, "pages": pgs})

    if not candidates:
        print("没有可用轨迹")
        return 1

    chosen = select_candidates(candidates, args.max)
    all_pages = set().union(*(set(c["pages"]) for c in candidates))
    covered = set().union(*(set(c["pages"]) for c in chosen)) if chosen else set()

    dst.mkdir(parents=True, exist_ok=True)
    for old in dst.glob("seed_*.json"):
        old.unlink()
    for i, c in enumerate(chosen, 1):
        t = c["trace"]
        t["app_pkg_harmony"] = args.bundle
        desc = "-".join(short(p) for p in c["pages"])[:60] or "flow"
        name = f"seed_{i:02d}_{desc}.json"
        (dst / name).write_text(
            json.dumps(t, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  ✓ {name}  ({c['n']} 步, 页面: {', '.join(c['pages'])})")

    print(f"\n共选 {len(chosen)} 条种子 → {dst}")
    print(f"页面覆盖: {len(covered)}/{len(all_pages)}  {sorted(all_pages)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""源端控件感知探索器（含录制，§6.4 / M6）。

机制：执行—提炼—再落地 的前两步（§2.2）——
每步 dump 安卓控件树 → 加权随机选可交互控件 → 原生执行 →
提炼为抽象事件（多重指纹 + 执行前截图 patch）→ 记录执行后状态基线。
"""
from __future__ import annotations

import logging
import os
import random
import time
from run_control import BudgetExceeded, check_budget
from typing import TYPE_CHECKING, Optional

from .config import Config
from .matcher import match
from .schemas import (AbstractEvent, TargetFingerprint, Trace, UNode,
                      save_json)
from .state import alpha, build_state_vector, compile_masks

if TYPE_CHECKING:  # pragma: no cover
    from .adapters.android import AndroidAdapter

logger = logging.getLogger("diff_tester")


def _node_key(n: UNode) -> tuple:
    """控件"身份"键，用于新颖性统计（同一控件跨步/跨轨迹去重）。"""
    cx, cy = n.center_rel()
    return (n.role, n.id, n.text[:30], n.desc[:30], round(cx, 2), round(cy, 2))


def _make_fingerprint(node: UNode, screen_png: Optional[str],
                      patch_path: Optional[str]) -> TargetFingerprint:
    saved_patch: Optional[str] = None
    if screen_png and patch_path and os.path.exists(screen_png):
        try:
            from PIL import Image
            x1, y1, x2, y2 = node.abs_bounds
            if x2 > x1 and y2 > y1:
                Image.open(screen_png).crop((x1, y1, x2, y2)).save(patch_path)
                saved_patch = patch_path
        except Exception as e:
            logger.debug("裁剪控件 patch 失败: %s", e)
    return TargetFingerprint(
        role=node.role, id_hint=node.id, text=node.text, desc=node.desc,
        rel_bounds=node.rel_bounds, patch_path=saved_patch,
    )


def explore(
    android: "AndroidAdapter",
    cfg: Config,
    out_dir: str,
    n_traces: int,
    max_steps: int,
    seeds: Optional[list[Trace]] = None,
    app_pkg_harmony: str = "",
) -> list[str]:
    """录制 n_traces 条轨迹，返回轨迹 JSON 路径列表。

    seeds：可选种子轨迹（轮流用作前缀，先自回放校验种子有效，再接随机探索）。
    """
    rng = random.Random(cfg.explorer.random_seed)
    masks = compile_masks(cfg.mask_patterns)
    traces_dir = os.path.join(out_dir, "traces")
    artifacts_root = os.path.join(out_dir, "artifacts")
    os.makedirs(traces_dir, exist_ok=True)

    visited: set[tuple] = set()      # 跨轨迹共享的新颖性记录
    out_paths: list[str] = []

    for k in range(n_traces):
        check_budget()
        trace_id = f"trace_{k:03d}"
        art_dir = os.path.join(artifacts_root, trace_id)
        os.makedirs(art_dir, exist_ok=True)
        logger.info("[record] %s 开始（max_steps=%d）", trace_id, max_steps)

        android.reset_app()
        trace = Trace(
            trace_id=trace_id,
            app_pkg_android=android.pkg,
            app_pkg_harmony=app_pkg_harmony,
            meta={
                "recorded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "device": android.serial or "default",
                "seed": None,
                "recorder": "explorer",
            },
        )
        trace.initial_state = alpha(android, masks, art_dir, "initial")

        step = 0
        seed_prefix_failed = False
        # ---- 种子前缀：在安卓自身树上定位并执行（自回放校验种子有效）----------
        if seeds:
            seed = seeds[k % len(seeds)]
            trace.meta.update({
                "seed": seed.trace_id,
                "seed_status": "pending",
                "seed_prefix_expected_steps": len(seed.events),
                "seed_prefix_completed_steps": 0,
            })
            step = _replay_seed_prefix(android, cfg, masks, seed, trace, art_dir)
            completed_steps = step if step >= 0 else len(trace.events)
            trace.meta["seed_prefix_completed_steps"] = completed_steps
            if step < 0:
                logger.warning("[record] %s 种子 %s 自回放失败，拒绝随机替代",
                               trace_id, seed.trace_id)
                trace.events.clear()
                trace.meta["seed_status"] = "failed"
                trace.meta["ended_by"] = "seed_prefix_failed"
                step = 0
                seed_prefix_failed = True
            else:
                trace.meta["seed_status"] = "completed"

        # ---- 随机探索 --------------------------------------------------------
        empty_rounds = 0
        while not seed_prefix_failed and step < max_steps:
            check_budget()
            step += 1
            tree = android.dump_tree()
            page = android.current_page()
            pre_state = build_state_vector(tree, page, masks)

            # 应用离开前台/崩溃 → 终止本轨迹
            if not android.app_alive():
                trace.meta["ended_by"] = "app_dead"
                break
            fg = android.current_pkg()
            if fg != android.pkg:
                trace.meta["ended_by"] = (
                    f"foreground_lost:{fg}" if fg else "foreground_unknown"
                )
                break

            candidates = [n for n in tree.iter_interactive() if n.rel_area() > 0]
            ev, node = _pick_event(rng, cfg, candidates, visited, step)
            if ev is None:
                empty_rounds += 1
                if empty_rounds >= 2:
                    trace.meta["ended_by"] = "no_candidates"
                    break
                ev = AbstractEvent(step=step, action="BACK", target=None)
                node = None
            else:
                empty_rounds = 0

            # 指纹要在执行前采集（§4.3）
            if node is not None:
                screen_png = os.path.join(art_dir, f"step{step}_screen.png")
                try:
                    android.screenshot(screen_png)
                except BudgetExceeded:
                    raise
                except Exception:
                    screen_png = None
                patch = os.path.join(art_dir, f"step{step}_target.png")
                ev.target = _make_fingerprint(node, screen_png, patch)
                visited.add(_node_key(node))
            ev.pre_state_hash = pre_state.hash()
            ev.pre_state = pre_state

            try:
                android.execute(ev, node)
            except Exception as e:
                if isinstance(e, BudgetExceeded):
                    raise
                logger.warning("[record] %s step%d 执行失败(%s)，终止本轨迹",
                               trace_id, step, e)
                trace.meta["ended_by"] = f"exec_error:{e}"
                break
            stable = android.wait_stable()
            post_pkg = android.current_pkg()
            if post_pkg != android.pkg:
                # Random exploration has no pre-declared external-surface
                # contract. Do not persist a system-owned or unknown window as
                # an application baseline for a later translation repair.
                trace.meta["ended_by"] = (
                    f"undeclared_external_surface:{post_pkg}"
                    if post_pkg else "post_state_ownership_unknown"
                )
                break
            ev.post_state = alpha(android, masks, art_dir, f"step{step}")
            if not stable:
                trace.meta.setdefault("unstable_steps", []).append(step)
            trace.events.append(ev)
            logger.info("[record] %s step=%d action=%s target=%s page=%s",
                        trace_id, step, ev.action,
                        (ev.target.text or ev.target.id_hint or ev.target.role)
                        if ev.target else "-",
                        ev.post_state.page)

            if ev.post_state.crash_sig:
                trace.meta["ended_by"] = f"crash:{ev.post_state.crash_sig}"
                break

        path = os.path.join(traces_dir, f"{trace_id}.json")
        save_json(trace.to_dict(), path)
        out_paths.append(path)
        logger.info("[record] %s 完成：%d 个事件 → %s", trace_id, len(trace.events), path)

    return out_paths


def _pick_event(
    rng: random.Random,
    cfg: Config,
    candidates: list[UNode],
    visited: set[tuple],
    step: int,
) -> tuple[Optional[AbstractEvent], Optional[UNode]]:
    """选择策略（§6.4）：新颖性加权 + 概率性系统事件。"""
    ec = cfg.explorer
    if not candidates:
        return None, None
    if rng.random() < ec.back_prob:
        return AbstractEvent(step=step, action="BACK", target=None), None

    weights = [ec.novelty_weight if _node_key(n) not in visited else ec.visited_weight
               for n in candidates]
    node = rng.choices(candidates, weights=weights, k=1)[0]

    if node.editable:
        text = rng.choice(ec.type_corpus)
        return AbstractEvent(step=step, action="TYPE", target=None,
                             params={"text": text}), node
    if node.role == "list" and rng.random() < ec.swipe_prob:
        return AbstractEvent(step=step, action="SWIPE", target=None,
                             params={"direction": rng.choice(["up", "down"]),
                                     "dist": 0.5}), node
    if rng.random() < ec.long_click_prob:
        return AbstractEvent(step=step, action="LONG_CLICK", target=None), node
    return AbstractEvent(step=step, action="CLICK", target=None), node


def _replay_seed_prefix(
    android: "AndroidAdapter",
    cfg: Config,
    masks: list,
    seed: Trace,
    trace: Trace,
    art_dir: str,
) -> int:
    """种子前缀自回放：逐事件按 matcher 在安卓自身树上定位并执行。

    成功返回已完成的步数；失败返回 -1。
    重新采集指纹与 post_state（以当前设备为准，保证基线新鲜）。
    """
    step = 0
    for sev in seed.events:
        check_budget()
        step += 1
        tree = android.dump_tree()
        page = android.current_page()
        pre_state = build_state_vector(tree, page, masks)

        node = None
        if sev.target is not None:
            m = match(sev.target, tree, sev.action, cfg.matcher)
            if m.kind != "MATCHED":
                logger.warning("[seed] step%d 定位失败(%s, score=%.2f)",
                               step, m.kind, m.score)
                return -1
            node = m.node

        ev = AbstractEvent(
            step=step,
            action=sev.action,
            target=None,
            params=dict(sev.params),
            external_surface=sev.external_surface,
        )
        if node is not None:
            screen_png = os.path.join(art_dir, f"step{step}_screen.png")
            try:
                android.screenshot(screen_png)
            except BudgetExceeded:
                raise
            except Exception:
                screen_png = None
            patch = os.path.join(art_dir, f"step{step}_target.png")
            ev.target = _make_fingerprint(node, screen_png, patch)
        ev.pre_state_hash = pre_state.hash()
        ev.pre_state = pre_state

        try:
            android.execute(ev, node)
        except Exception as e:
            if isinstance(e, BudgetExceeded):
                raise
            logger.warning("[seed] step%d 执行失败: %s", step, e)
            return -1
        if not android.wait_stable():
            logger.warning("[seed] step%d 执行后界面未稳定", step)
            return -1

        foreground = android.current_pkg()
        if not foreground:
            logger.warning("[seed] step%d 无法确认执行后前台所有权", step)
            return -1
        if foreground != android.pkg:
            contract = ev.external_surface
            if contract is None:
                logger.warning(
                    "[seed] step%d 前台切换到 %s，但事件未声明外部表面协议",
                    step, foreground,
                )
                return -1
            recovery = AbstractEvent(
                step=step,
                action=contract.recovery_action,
                target=None,
            )
            try:
                android.execute(recovery, None)
            except Exception as e:
                if isinstance(e, BudgetExceeded):
                    raise
                logger.warning("[seed] step%d 外部表面恢复失败: %s", step, e)
                return -1
            if not android.wait_stable():
                logger.warning("[seed] step%d 外部表面恢复后界面未稳定", step)
                return -1
            recovered_pkg = android.current_pkg()
            if recovered_pkg != android.pkg:
                logger.warning(
                    "[seed] step%d 恢复后前台仍非应用: %s",
                    step, recovered_pkg or "<unknown>",
                )
                return -1

        ev.post_state = alpha(android, masks, art_dir, f"step{step}")
        if ev.post_state.external_surface is not None:
            logger.warning(
                "[seed] step%d post_state 仍属于外部表面 %s",
                step, ev.post_state.external_surface,
            )
            return -1
        trace.events.append(ev)
    return step

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from multiprocessing import Event, get_context
from typing import Any, Dict, List, Tuple

from trading.config import load_config
from trading.utils.env import load_env
from trading.ipc.events import ControlCommand, Heartbeat, JournalEvent
from trading.ipc.queues import try_put
from trading.processes.context import ProcessContext
from trading.processes.core import run_core
from trading.processes.exec import run_exec
from trading.processes.journal import run_journal
from trading.processes.md import run_md
from trading.processes.impact import run_impact
from trading.processes.control import run_control


def _cfg(cfg: Dict[str, Any], path: str, default: Any) -> Any:
    cur = cfg
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _resolve_mp_start_method(cfg: Dict[str, Any]) -> str:
    allowed = {"spawn", "fork", "forkserver"}
    raw = str(os.getenv("TRADING_MP_START_METHOD", _cfg(cfg, "ipc.mp_start_method", "")) or "").strip().lower()
    if raw in allowed:
        return raw
    if sys.platform.startswith("linux"):
        return "fork"
    return "spawn"


def _exec_process_enabled(cfg: Dict[str, Any]) -> bool:
    raw = _cfg(cfg, "exec.enabled", True)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _make_context(cfg: Dict[str, Any], mode: str) -> tuple[ProcessContext, Any]:
    start_method = _resolve_mp_start_method(cfg)
    try:
        mp_ctx = get_context(start_method)
    except Exception:
        mp_ctx = get_context("spawn")
    stop_event = mp_ctx.Event()

    q_market_core = mp_ctx.Queue(maxsize=int(_cfg(cfg, "ipc.market_queue_size", 1)))
    q_market_exec = mp_ctx.Queue(maxsize=int(_cfg(cfg, "ipc.market_queue_size", 1)))
    q_order_intent = mp_ctx.Queue(maxsize=int(_cfg(cfg, "ipc.order_queue_size", 100)))
    q_exec_report = mp_ctx.Queue(maxsize=int(_cfg(cfg, "ipc.exec_report_queue_size", 100)))
    q_journal = mp_ctx.Queue(maxsize=int(_cfg(cfg, "ipc.journal_queue_size", 10000)))
    q_control_core = mp_ctx.Queue(maxsize=int(_cfg(cfg, "ipc.control_queue_size", 100)))
    q_control_exec = mp_ctx.Queue(maxsize=int(_cfg(cfg, "ipc.control_queue_size", 100)))
    q_telemetry = mp_ctx.Queue(maxsize=int(_cfg(cfg, "ipc.telemetry_queue_size", 1000)))
    q_heartbeat = mp_ctx.Queue(maxsize=int(_cfg(cfg, "ipc.heartbeat_queue_size", 1000)))
    q_impact_core = mp_ctx.Queue(maxsize=int(_cfg(cfg, "ipc.impact_queue_size", _cfg(cfg, "ipc.news_queue_size", 1))))

    ctx = ProcessContext(
        mode=mode,
        config=cfg,
        stop_event=stop_event,
        q_market_core=q_market_core,
        q_market_exec=q_market_exec,
        q_order_intent=q_order_intent,
        q_exec_report=q_exec_report,
        q_journal=q_journal,
        q_control_core=q_control_core,
        q_control_exec=q_control_exec,
        q_telemetry=q_telemetry,
        q_heartbeat=q_heartbeat,
        q_impact_core=q_impact_core,
    )
    return ctx, mp_ctx


def _debug_watchdog_enabled(cfg: Dict[str, Any]) -> bool:
    raw = _cfg(cfg, "ipc.debug_watchdog", os.getenv("TRADING_DEBUG_WATCHDOG", "0"))
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _debug_log(enabled: bool, message: str) -> None:
    if not enabled:
        return
    ts = time_now().isoformat()
    print(f"{ts} [launch] {message}", file=sys.stderr, flush=True)


def _send_journal(ctx: ProcessContext, event_type: str, payload: Dict[str, Any]) -> None:
    try_put(ctx.q_journal, JournalEvent(ts=time_now(), event_type=event_type, payload=payload))


def _drain_heartbeat_queue(ctx: ProcessContext, last_hb: Dict[str, float], seen_heartbeat: Dict[str, bool]) -> int:
    drained = 0
    try:
        hb: Heartbeat = ctx.q_heartbeat.get(timeout=1.0)
    except Exception:
        return drained

    while True:
        drained += 1
        last_hb[hb.process] = time.time()
        if hb.process in seen_heartbeat:
            seen_heartbeat[hb.process] = True
        try:
            hb = ctx.q_heartbeat.get_nowait()
        except Exception:
            break
    return drained


def _watchdog_log(
    ctx: ProcessContext,
    *,
    action: str,
    reason: str,
    child: str | None = None,
    exitcode: int | None = None,
    hb_age_sec: float | None = None,
    startup_age_sec: float | None = None,
) -> None:
    payload: Dict[str, Any] = {
        "action": str(action or "").strip().lower(),
        "reason": str(reason or "").strip().lower(),
    }
    if child:
        payload["child"] = str(child).strip().lower()
    if exitcode is not None:
        payload["exitcode"] = int(exitcode)
    if hb_age_sec is not None:
        payload["heartbeat_age_sec"] = float(hb_age_sec)
    if startup_age_sec is not None:
        payload["startup_age_sec"] = float(startup_age_sec)
    _send_journal(ctx, "launch_watchdog", payload)

    details = [f"action={payload['action']}", f"reason={payload['reason']}"]
    if child:
        details.append(f"child={payload['child']}")
    if exitcode is not None:
        details.append(f"exitcode={int(exitcode)}")
    if hb_age_sec is not None:
        details.append(f"heartbeat_age_sec={float(hb_age_sec):.3f}")
    if startup_age_sec is not None:
        details.append(f"startup_age_sec={float(startup_age_sec):.3f}")
    print(f"{time_now().isoformat()} [launch] {' '.join(details)}", file=sys.stderr, flush=True)


def _start_processes(ctx: ProcessContext, mp_ctx: Any) -> List[Any]:
    processes = []
    processes.append(mp_ctx.Process(target=run_md, args=(ctx,), name="md"))
    processes.append(mp_ctx.Process(target=run_core, args=(ctx,), name="core"))
    if _exec_process_enabled(ctx.config):
        processes.append(mp_ctx.Process(target=run_exec, args=(ctx,), name="exec"))
    processes.append(mp_ctx.Process(target=run_journal, args=(ctx,), name="journal"))
    if bool(_cfg(ctx.config, "impact.enabled", False)):
        processes.append(mp_ctx.Process(target=run_impact, args=(ctx,), name="impact"))

    if bool(_cfg(ctx.config, "control.enabled", False)):
        processes.append(mp_ctx.Process(target=run_control, args=(ctx,), name="control"))

    for p in processes:
        p.start()
    return processes


def _spawn_process(mp_ctx: Any, name: str, ctx: ProcessContext):
    target = {
        "md": run_md,
        "core": run_core,
        "exec": run_exec,
        "journal": run_journal,
        "impact": run_impact,
        "control": run_control,
    }.get(name)
    if target is None:
        return None
    proc = mp_ctx.Process(target=target, args=(ctx,), name=name)
    proc.start()
    return proc


def _force_stop_processes(processes: List[Any], *, join_timeout: float = 2.0) -> None:
    """Best-effort teardown: join, then terminate, then kill lingering children."""
    for p in processes:
        if p.is_alive():
            p.join(timeout=join_timeout)
    for p in processes:
        if p.is_alive():
            p.terminate()
    for p in processes:
        if p.is_alive():
            p.join(timeout=join_timeout)
    for p in processes:
        if p.is_alive():
            try:
                p.kill()
            except Exception:
                pass
    for p in processes:
        if p.is_alive():
            p.join(timeout=1.0)


def _shutdown(ctx: ProcessContext) -> None:
    ctx.stop_event.set()
    try_put(ctx.q_control_core, ControlCommand(ts=time_now(), action="STOP", reason="shutdown"))
    try_put(ctx.q_control_exec, ControlCommand(ts=time_now(), action="STOP", reason="shutdown"))
    try_put(ctx.q_control_core, ControlCommand(ts=time_now(), action="CANCEL_ALL", reason="shutdown"))
    try_put(ctx.q_control_exec, ControlCommand(ts=time_now(), action="CANCEL_ALL", reason="shutdown"))


def time_now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def main() -> None:
    parser = build_parser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--env", default=".env")
    parser.add_argument("--pidfile", default=None)
    args = parser.parse_args()

    load_env(args.env)
    cfg = load_config(args.config).raw
    cfg.setdefault("runtime", {})
    cfg["runtime"].setdefault("config_path", os.path.abspath(args.config))
    debug_watchdog = _debug_watchdog_enabled(cfg)
    ctx, mp_ctx = _make_context(cfg, args.mode)

    processes = _start_processes(ctx, mp_ctx)
    procs = {p.name: p for p in processes}
    processes = list(procs.values())
    for name, proc in procs.items():
        _debug_log(debug_watchdog, f"started {name} pid={proc.pid}")

    exec_enabled = _exec_process_enabled(cfg)
    critical = {"md", "core"}
    if exec_enabled:
        critical.add("exec")
    base_names = ["md", "core", "journal", "control"]
    if exec_enabled:
        base_names.append("exec")
    if bool(_cfg(cfg, "impact.enabled", False)):
        base_names.append("impact")
    last_hb: Dict[str, float] = {name: time.time() for name in base_names}
    heartbeat_timeout = float(_cfg(cfg, "ipc.heartbeat_timeout", 5.0))
    startup_grace_sec = float(
        _cfg(
            cfg,
            "ipc.startup_grace_sec",
            max(
                heartbeat_timeout * 3.0,
                30.0,
                float(_cfg(cfg, "core.warmup.timeout_sec", 0.0)) + 5.0,
            ),
        )
    )
    restart_critical = bool(_cfg(cfg, "ipc.restart_critical", False))
    auto_resume = bool(_cfg(cfg, "ipc.auto_resume", False))
    seen_heartbeat: Dict[str, bool] = {name: False for name in base_names}
    child_started_at: Dict[str, float] = {name: time.time() for name in base_names}

    pidfile = args.pidfile or _cfg(cfg, "ipc.pidfile", None)
    if pidfile:
        import json
        os.makedirs(os.path.dirname(pidfile), exist_ok=True)
        with open(pidfile, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "pid": os.getpid(),
                    "children": {name: p.pid for name, p in procs.items()},
                },
                f,
                indent=2,
            )

    def handle_sig(_sig, _frame):
        _debug_log(debug_watchdog, f"signal {_sig} received; shutting down")
        _watchdog_log(ctx, action="shutdown_lane", reason="signal", exitcode=int(_sig))
        _shutdown(ctx)

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    while True:
        processes = list(procs.values())
        # break if all processes stopped
        if all(not p.is_alive() for p in processes):
            break

        # Drain all queued heartbeats so one busy child cannot starve the others.
        _drain_heartbeat_queue(ctx, last_hb, seen_heartbeat)

        now = time.time()
        for name, ts in list(last_hb.items()):
            hb_age = now - ts
            startup_age = now - child_started_at.get(name, now)
            if (
                name in critical
                and not seen_heartbeat.get(name, False)
                and startup_age <= startup_grace_sec
            ):
                continue
            if name in critical and hb_age > heartbeat_timeout:
                should_restart_child = bool(name == "exec" or (restart_critical and name in {"md", "exec"}))
                if should_restart_child:
                    _debug_log(
                        debug_watchdog,
                        f"heartbeat stale for {name}; age_sec={hb_age:.3f}; startup_age_sec={startup_age:.3f}; restarting child",
                    )
                    _watchdog_log(
                        ctx,
                        action="restart_child",
                        reason="heartbeat_stale",
                        child=name,
                        hb_age_sec=hb_age,
                        startup_age_sec=startup_age,
                    )
                    try_put(ctx.q_control_core, ControlCommand(ts=time_now(), action="PAUSE", reason=f"{name}_stale"))
                    try_put(ctx.q_control_exec, ControlCommand(ts=time_now(), action="PAUSE", reason=f"{name}_stale"))
                    if procs.get(name) and procs[name].is_alive():
                        procs[name].terminate()
                        procs[name].join(timeout=2.0)
                    procs[name] = _spawn_process(mp_ctx, name, ctx)
                    processes = list(procs.values())
                    last_hb[name] = time.time()
                    child_started_at[name] = time.time()
                    seen_heartbeat[name] = False
                    if pidfile:
                        import json
                        with open(pidfile, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        data["children"][name] = procs[name].pid if procs[name] else None
                        with open(pidfile, "w", encoding="utf-8") as f:
                            json.dump(data, f, indent=2)
                    if auto_resume:
                        try_put(ctx.q_control_core, ControlCommand(ts=time_now(), action="RESUME", reason=f"{name}_restart"))
                else:
                    proc = procs.get(name)
                    exitcode = None if proc is None else proc.exitcode
                    _debug_log(
                        debug_watchdog,
                        f"heartbeat stale for {name}; age_sec={hb_age:.3f}; startup_age_sec={startup_age:.3f}; exitcode={exitcode}; shutting down",
                    )
                    _watchdog_log(
                        ctx,
                        action="shutdown_lane",
                        reason="heartbeat_stale",
                        child=name,
                        exitcode=exitcode,
                        hb_age_sec=hb_age,
                        startup_age_sec=startup_age,
                    )
                    _shutdown(ctx)
                    break

        # detect critical process crash
        for p in list(processes):
            if not p.is_alive() and p.name in critical:
                should_restart_child = bool(p.name == "exec" or (restart_critical and p.name in {"md", "exec"}))
                if should_restart_child:
                    _debug_log(
                        debug_watchdog,
                        f"critical child {p.name} exited exitcode={p.exitcode}; restarting",
                    )
                    _watchdog_log(
                        ctx,
                        action="restart_child",
                        reason="critical_child_exit",
                        child=p.name,
                        exitcode=p.exitcode,
                    )
                    try_put(ctx.q_control_core, ControlCommand(ts=time_now(), action="PAUSE", reason=f"{p.name}_crash"))
                    try_put(ctx.q_control_exec, ControlCommand(ts=time_now(), action="PAUSE", reason=f"{p.name}_crash"))
                    procs[p.name] = _spawn_process(mp_ctx, p.name, ctx)
                    processes = list(procs.values())
                    last_hb[p.name] = time.time()
                    child_started_at[p.name] = time.time()
                    seen_heartbeat[p.name] = False
                    if pidfile:
                        import json
                        with open(pidfile, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        data["children"][p.name] = procs[p.name].pid if procs[p.name] else None
                        with open(pidfile, "w", encoding="utf-8") as f:
                            json.dump(data, f, indent=2)
                    if auto_resume:
                        try_put(ctx.q_control_core, ControlCommand(ts=time_now(), action="RESUME", reason=f"{p.name}_restart"))
                else:
                    _debug_log(
                        debug_watchdog,
                        f"critical child {p.name} exited exitcode={p.exitcode}; shutting down",
                    )
                    _watchdog_log(
                        ctx,
                        action="shutdown_lane",
                        reason="critical_child_exit",
                        child=p.name,
                        exitcode=p.exitcode,
                    )
                    _shutdown(ctx)
                    break

        if ctx.stop_event.is_set():
            _debug_log(debug_watchdog, "stop_event set; beginning teardown")
            break

    _force_stop_processes(processes)
    for p in processes:
        _debug_log(debug_watchdog, f"child {p.name} final_exitcode={p.exitcode}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["live", "paper", "sim"], default="paper")
    return parser


if __name__ == "__main__":
    main()

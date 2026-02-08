from __future__ import annotations

import argparse
import signal
import time
from multiprocessing import Event, get_context
from typing import Any, Dict, List, Tuple

from trading.config import load_config
from trading.utils.env import load_env
from trading.ipc.events import ControlCommand, Heartbeat
from trading.ipc.queues import try_put
from trading.processes.context import ProcessContext
from trading.processes.core import run_core
from trading.processes.exec import run_exec
from trading.processes.journal import run_journal
from trading.processes.md import run_md
from trading.processes.control import run_control


def _cfg(cfg: Dict[str, Any], path: str, default: Any) -> Any:
    cur = cfg
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _make_context(cfg: Dict[str, Any], mode: str) -> tuple[ProcessContext, Any]:
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
    )
    return ctx, mp_ctx


def _start_processes(ctx: ProcessContext, mp_ctx: Any) -> List[Any]:
    processes = []
    processes.append(mp_ctx.Process(target=run_md, args=(ctx,), name="md"))
    processes.append(mp_ctx.Process(target=run_core, args=(ctx,), name="core"))
    processes.append(mp_ctx.Process(target=run_exec, args=(ctx,), name="exec"))
    processes.append(mp_ctx.Process(target=run_journal, args=(ctx,), name="journal"))

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
        "control": run_control,
    }.get(name)
    if target is None:
        return None
    proc = mp_ctx.Process(target=target, args=(ctx,), name=name)
    proc.start()
    return proc


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
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["live", "paper"], default="paper")
    parser.add_argument("--config", required=True)
    parser.add_argument("--env", default=".env")
    parser.add_argument("--pidfile", default=None)
    args = parser.parse_args()

    load_env(args.env)
    cfg = load_config(args.config).raw
    ctx, mp_ctx = _make_context(cfg, args.mode)

    processes = _start_processes(ctx, mp_ctx)
    procs = {p.name: p for p in processes}
    processes = list(procs.values())

    critical = {"md", "core", "exec"}
    last_hb: Dict[str, float] = {name: time.time() for name in ["md", "core", "exec", "journal", "control"]}
    heartbeat_timeout = float(_cfg(cfg, "ipc.heartbeat_timeout", 5.0))
    restart_critical = bool(_cfg(cfg, "ipc.restart_critical", False))
    auto_resume = bool(_cfg(cfg, "ipc.auto_resume", False))

    pidfile = args.pidfile or _cfg(cfg, "ipc.pidfile", None)
    if pidfile:
        import json
        import os
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
        _shutdown(ctx)

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    while True:
        processes = list(procs.values())
        # break if all processes stopped
        if all(not p.is_alive() for p in processes):
            break

        # watchdog heartbeats
        try:
            hb: Heartbeat = ctx.q_heartbeat.get(timeout=1.0)
            last_hb[hb.process] = time.time()
        except Exception:
            pass

        now = time.time()
        for name, ts in list(last_hb.items()):
            if name in critical and (now - ts) > heartbeat_timeout:
                if restart_critical and name in {"md", "exec"}:
                    try_put(ctx.q_control_core, ControlCommand(ts=time_now(), action="PAUSE", reason=f"{name}_stale"))
                    try_put(ctx.q_control_exec, ControlCommand(ts=time_now(), action="PAUSE", reason=f"{name}_stale"))
                    if procs.get(name) and procs[name].is_alive():
                        procs[name].terminate()
                        procs[name].join(timeout=2.0)
                    procs[name] = _spawn_process(mp_ctx, name, ctx)
                    processes = list(procs.values())
                    last_hb[name] = time.time()
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
                    _shutdown(ctx)

        # detect critical process crash
        for p in list(processes):
            if not p.is_alive() and p.name in critical:
                if restart_critical and p.name in {"md", "exec"}:
                    try_put(ctx.q_control_core, ControlCommand(ts=time_now(), action="PAUSE", reason=f"{p.name}_crash"))
                    try_put(ctx.q_control_exec, ControlCommand(ts=time_now(), action="PAUSE", reason=f"{p.name}_crash"))
                    procs[p.name] = _spawn_process(mp_ctx, p.name, ctx)
                    processes = list(procs.values())
                    last_hb[p.name] = time.time()
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
                    _shutdown(ctx)

        if ctx.stop_event.is_set():
            break

    for p in processes:
        if p.is_alive():
            p.join(timeout=2.0)


if __name__ == "__main__":
    main()

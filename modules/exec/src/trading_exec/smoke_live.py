from __future__ import annotations

import argparse
import time
from dataclasses import asdict
from datetime import datetime, timezone
from multiprocessing import get_context
from queue import Empty
from typing import Any, Dict, List, Optional

from trading.config import load_config
from trading.ipc.events import ExecutionReport, OrderIntent
from trading.processes.context import ProcessContext
from trading.processes.exec import run_exec
from trading.processes.journal import run_journal
from trading.utils.env import load_env


def _cfg(d: Dict[str, Any], path: str, default: Any) -> Any:
    cur: Any = d
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
    q_exec_report = mp_ctx.Queue(maxsize=int(_cfg(cfg, "ipc.exec_report_queue_size", 200)))
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
        q_impact_core=None,
    )
    return ctx, mp_ctx


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _drain_reports(ctx: ProcessContext, max_items: int = 50) -> list[Any]:
    out: list[Any] = []
    for _ in range(max_items):
        try:
            out.append(ctx.q_exec_report.get_nowait())
        except Exception:
            break
    return out


def _wait_for_report(
    ctx: ProcessContext,
    *,
    deadline_ts: float,
    want_statuses: Optional[set[str]] = None,
) -> list[Any]:
    want_statuses = want_statuses or set()
    got: list[Any] = []
    while time.time() < deadline_ts and not ctx.stop_event.is_set():
        try:
            item = ctx.q_exec_report.get(timeout=0.25)
        except Empty:
            continue
        got.append(item)
        if isinstance(item, ExecutionReport) and item.status in want_statuses:
            return got
    return got


def run_smoke(
    *,
    config_path: str,
    env_path: str,
    qty_btc: float,
    mode: str,
    canary: bool,
    timeout_sec: float,
) -> int:
    load_env(env_path)
    cfg = load_config(config_path).raw

    # Override exec.canary_mode at runtime for smoke.
    cfg = dict(cfg)
    exec_cfg = dict(cfg.get("exec", {})) if isinstance(cfg.get("exec", {}), dict) else {}
    exec_cfg["canary_mode"] = bool(canary)
    cfg["exec"] = exec_cfg

    ctx, mp_ctx = _make_context(cfg, mode=mode)

    p_journal = mp_ctx.Process(target=run_journal, args=(ctx,), name="journal")
    p_exec = mp_ctx.Process(target=run_exec, args=(ctx,), name="exec")
    p_journal.start()
    p_exec.start()

    # Give WS threads a short moment to subscribe and the startup snapshot to complete.
    time.sleep(1.0)
    _drain_reports(ctx)

    buy_id = f"smoke_buy_{int(time.time()*1000)}"
    sell_id = f"smoke_sell_{int(time.time()*1000)}"

    ctx.q_order_intent.put(
        OrderIntent(
            ts=_now(),
            side="buy",
            qty_btc=float(qty_btc),
            order_type="market",
            client_id=buy_id,
            reason="smoke_live",
        )
    )

    got = _wait_for_report(ctx, deadline_ts=time.time() + timeout_sec, want_statuses={"OPEN", "VALIDATED", "REJECTED"})
    # If canary, we expect VALIDATED. If live, we at least expect OPEN/REJECTED quickly.
    if not any(isinstance(x, ExecutionReport) for x in got):
        print("smoke_failed:no_exec_report_for_buy")
        ctx.stop_event.set()
        p_exec.join(timeout=2.0)
        p_journal.join(timeout=2.0)
        return 2

    if canary:
        ok = any(isinstance(x, ExecutionReport) and x.order_id == buy_id and x.status == "VALIDATED" for x in got)
        if not ok:
            reports = [
                {"order_id": x.order_id, "status": x.status, "reason": x.reason}
                for x in got
                if isinstance(x, ExecutionReport)
            ]
            print("smoke_canary_reports", reports[:20])
        print("smoke_canary_buy_validated", ok)
        ctx.stop_event.set()
        p_exec.join(timeout=2.0)
        p_journal.join(timeout=2.0)
        return 0 if ok else 3

    # For real live mode:
    # 1) Wait for a FILLED signal (reconcile/openOrders) to confirm the trade executed.
    # 2) Then wait for ownTrades Fill truth for that txid (fees etc).
    deadline = time.time() + timeout_sec
    filled_qty = 0.0
    buy_txid: str | None = None
    while time.time() < deadline and not ctx.stop_event.is_set():
        try:
            item = ctx.q_exec_report.get(timeout=0.5)
        except Empty:
            continue
        if isinstance(item, ExecutionReport) and item.status == "FILLED":
            buy_txid = item.order_id
            break
        if hasattr(item, "qty_btc") and hasattr(item, "order_id") and not isinstance(item, ExecutionReport):
            # Fill truth arrived fast enough; accept it.
            filled_qty += float(getattr(item, "qty_btc", 0.0) or 0.0)
            if filled_qty + 1e-12 >= qty_btc:
                buy_txid = str(getattr(item, "order_id", "")) or None
                break

    if buy_txid is None:
        print("smoke_failed:buy_not_filled", filled_qty)
        ctx.stop_event.set()
        p_exec.join(timeout=2.0)
        p_journal.join(timeout=2.0)
        return 4

    # Wait briefly for ownTrades Fill truth for the buy txid (best-effort within timeout).
    truth_deadline = time.time() + min(timeout_sec, 60.0)
    while time.time() < truth_deadline and not ctx.stop_event.is_set():
        try:
            item = ctx.q_exec_report.get(timeout=0.5)
        except Empty:
            continue
        if hasattr(item, "qty_btc") and hasattr(item, "order_id") and not isinstance(item, ExecutionReport):
            if str(getattr(item, "order_id", "")) == buy_txid:
                break

    ctx.q_order_intent.put(
        OrderIntent(
            ts=_now(),
            side="sell",
            qty_btc=float(qty_btc),
            order_type="market",
            client_id=sell_id,
            reason="smoke_live",
        )
    )

    deadline = time.time() + timeout_sec
    sell_filled = 0.0
    sell_txid: str | None = None
    while time.time() < deadline and not ctx.stop_event.is_set():
        try:
            item = ctx.q_exec_report.get(timeout=0.5)
        except Empty:
            continue
        if isinstance(item, ExecutionReport) and item.status == "FILLED":
            sell_txid = item.order_id
            break
        if hasattr(item, "qty_btc") and hasattr(item, "order_id") and not isinstance(item, ExecutionReport):
            sell_filled += float(getattr(item, "qty_btc", 0.0) or 0.0)
            if sell_filled + 1e-12 >= qty_btc:
                sell_txid = str(getattr(item, "order_id", "")) or None
                break

    if sell_txid is None:
        print("smoke_failed:sell_not_filled", sell_filled)
        ctx.stop_event.set()
        p_exec.join(timeout=2.0)
        p_journal.join(timeout=2.0)
        return 5

    # Best-effort truth wait for sell txid.
    truth_deadline = time.time() + min(timeout_sec, 60.0)
    while time.time() < truth_deadline and not ctx.stop_event.is_set():
        try:
            item = ctx.q_exec_report.get(timeout=0.5)
        except Empty:
            continue
        if hasattr(item, "qty_btc") and hasattr(item, "order_id") and not isinstance(item, ExecutionReport):
            if str(getattr(item, "order_id", "")) == sell_txid:
                break

    print("smoke_live_buy_sell_ok", True)
    ctx.stop_event.set()
    p_exec.join(timeout=2.0)
    p_journal.join(timeout=2.0)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Exec live smoke: inject 1 market buy + 1 market sell and wait for ownTrades fills.")
    ap.add_argument("--config", required=True, help="Path to runtime YAML (e.g. configs/live.yaml).")
    ap.add_argument("--env", default=".env", help="Path to .env file with KRAKEN_API_KEY/KRAKEN_API_SECRET.")
    ap.add_argument("--qty-btc", type=float, default=0.00005)
    ap.add_argument("--timeout-sec", type=float, default=30.0)
    ap.add_argument("--mode", choices=["live", "paper"], default="live")
    ap.add_argument("--canary", action="store_true", help="Use validate-only orders (no real trade).")
    args = ap.parse_args()

    rc = run_smoke(
        config_path=args.config,
        env_path=args.env,
        qty_btc=float(args.qty_btc),
        mode=args.mode,
        canary=bool(args.canary),
        timeout_sec=float(args.timeout_sec),
    )
    raise SystemExit(rc)


if __name__ == "__main__":
    main()

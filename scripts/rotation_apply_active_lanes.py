#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import time
import urllib.request
from pathlib import Path
import sys
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from trading.rotation_strategy_runtime import (
    ROTATION_RUNTIME_CONFIG_VERSION,
    build_rotation_runtime_config,
    normalize_rotation_strategy_name,
    normalize_symbol_strategy_map,
)
from trading.binance.rest import BinanceRestClient
from trading.rotation_universe import LANE_POOL, POOL, build_lanes

ACTIVE_FILE = REPO_ROOT / "configs" / "rotation_active_lanes.json"
MANUAL_LANES_FILE = REPO_ROOT / "configs" / "rotation_manual_lanes.json"
LANES = build_lanes(LANE_POOL)
RUNTIME_CONFIG_DIR = REPO_ROOT / "logs" / "rotation_runtime_configs"
FALLBACK_TEMPLATE_SYMBOL = "XLM"
PROC_ROOT = Path("/proc")
LANE_RUNTIME_REQUIRED_ENV: dict[str, str] = {
    # Keep shadow/watch lanes lightweight: control API stays on, optional per-lane GUIs off.
    "START_IMPACT_CONSOLE": "0",
    "START_JOURNAL_GUI": "0",
    "START_CORE_GUI": "0",
    "START_MD_GUI": "0",
    "START_EXEC": "0",
}
try:
    INVENTORY_PROTECT_MIN_NOTIONAL_EUR = max(
        0.0, float(os.getenv("ROTATION_INVENTORY_PROTECT_MIN_NOTIONAL_EUR", "1.0"))
    )
except ValueError:
    INVENTORY_PROTECT_MIN_NOTIONAL_EUR = 1.0


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        value = int(str(os.getenv(name, str(default))).strip())
    except Exception:
        value = int(default)
    return max(int(minimum), value)


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        value = float(str(os.getenv(name, str(default))).strip())
    except Exception:
        value = float(default)
    return max(float(minimum), value)


WATCHDOG_MIN_HEARTBEAT_TIMEOUT_SEC = _env_float(
    "ROTATION_LANE_MIN_HEARTBEAT_TIMEOUT_SEC",
    45.0,
    minimum=5.0,
)
WATCHDOG_MIN_STARTUP_GRACE_SEC = _env_float(
    "ROTATION_LANE_MIN_STARTUP_GRACE_SEC",
    180.0,
    minimum=15.0,
)
IPC_MAX_JOURNAL_QUEUE_SIZE = _env_int(
    "ROTATION_LANE_MAX_JOURNAL_QUEUE_SIZE",
    600,
    minimum=50,
)
IPC_MAX_TELEMETRY_QUEUE_SIZE = _env_int(
    "ROTATION_LANE_MAX_TELEMETRY_QUEUE_SIZE",
    120,
    minimum=20,
)
IPC_MAX_HEARTBEAT_QUEUE_SIZE = _env_int(
    "ROTATION_LANE_MAX_HEARTBEAT_QUEUE_SIZE",
    64,
    minimum=8,
)
ROTATION_UNIT_RE = re.compile(r"codex-rotation-([a-z0-9]+)\.service")
LANE_UNIT_TIMEOUT_SEC = 20.0


def _lane_base_config_path(symbol: str) -> Path:
    return (REPO_ROOT / str(LANES[symbol]["config"])).resolve()


def _lane_runtime_config_path(symbol: str) -> Path:
    slug = str(LANES[symbol]["slug"])
    return (RUNTIME_CONFIG_DIR / f"{slug}_runtime.yaml").resolve()


def _lane_unit_name(symbol: str) -> str:
    return f"codex-rotation-{LANES[symbol]['slug']}.service"


def list_running_rotation_symbols() -> list[str]:
    try:
        out = subprocess.check_output(
            [
                "systemctl",
                "--user",
                "list-units",
                "codex-rotation-*.service",
                "--state=running",
                "--no-pager",
            ],
            cwd=REPO_ROOT,
            text=True,
            timeout=5,
        )
    except Exception:
        return []

    symbols: list[str] = []
    for line in out.splitlines():
        match = ROTATION_UNIT_RE.search(line)
        if not match:
            continue
        slug = str(match.group(1)).strip().lower()
        if slug in {"selector", "status", "http", "watch-pool-refresh"}:
            continue
        symbol = slug.upper()
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return symbols


def _state_row_strategy_map(state: dict) -> dict[str, str]:
    rows_source = state.get("all_rows")
    if not isinstance(rows_source, list):
        rows_source = state.get("rows")
    if not isinstance(rows_source, list):
        return {}
    out: dict[str, str] = {}
    for row in rows_source:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol", "")).strip().upper()
        strategy = normalize_rotation_strategy_name(row.get("strategy_primary"))
        if symbol and strategy:
            out[symbol] = strategy
    return out


def _resolve_lane_strategy(
    symbol: str,
    *,
    selected_strategy_map: dict[str, str],
    row_strategy_map: dict[str, str],
) -> tuple[str, str]:
    if symbol in selected_strategy_map:
        return selected_strategy_map[symbol], "selected_strategy_map"
    if symbol in row_strategy_map:
        return row_strategy_map[symbol], "row.strategy_primary"
    return "", "base_config"


def _load_raw_yaml_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"config must be a mapping: {path}")
    return data


def _fallback_template_config_path() -> Path:
    return _lane_base_config_path(FALLBACK_TEMPLATE_SYMBOL)


def _clone_lane_template_config(symbol: str) -> dict:
    template_path = _fallback_template_config_path()
    template_cfg = _load_raw_yaml_config(template_path)
    pair = f"{symbol}/USDC"
    slug = str(LANES[symbol]["slug"])
    control_port = int(LANES[symbol]["ports"][0])
    impact_port = control_port + 1

    md = template_cfg.setdefault("md", {})
    if isinstance(md, dict):
        md["pair"] = pair

    exec_cfg = template_cfg.setdefault("exec", {})
    if isinstance(exec_cfg, dict):
        exec_cfg["pair"] = pair

    impact = template_cfg.setdefault("impact", {})
    if isinstance(impact, dict):
        impact["symbol"] = pair
        impact["api_url"] = (
            f"http://127.0.0.1:{impact_port}/signal/multi-asset-probability"
            f"?target={symbol}&lookback=72h"
        )

    control = template_cfg.setdefault("control", {})
    if isinstance(control, dict):
        control["port"] = control_port

    live = template_cfg.setdefault("live", {})
    if isinstance(live, dict):
        live["symbol"] = pair

    journal = template_cfg.setdefault("journal", {})
    if isinstance(journal, dict):
        journal["db_path"] = f"logs/journal_live_binance_{slug}_usdc_rotation.db"
        journal["json_path"] = f"logs/journal_live_binance_{slug}_usdc_rotation.jsonl"

    return template_cfg


def _configured_min_entry_notional(symbol: str) -> float:
    runtime_config_path = _lane_runtime_config_path(symbol)
    try:
        runtime_cfg = _load_raw_yaml_config(runtime_config_path)
    except Exception:
        return 0.0
    try:
        return max(0.0, float(((runtime_cfg.get("exec") or {}).get("min_entry_notional_eur") or 0.0)))
    except Exception:
        return 0.0


def _ensure_lane_base_config(symbol: str) -> Path:
    base_config_path = _lane_base_config_path(symbol)
    try:
        _load_raw_yaml_config(base_config_path)
        return base_config_path
    except Exception:
        repaired_cfg = _clone_lane_template_config(symbol)
        rendered = yaml.safe_dump(repaired_cfg, sort_keys=False)
        base_config_path.parent.mkdir(parents=True, exist_ok=True)
        base_config_path.write_text(rendered, encoding="utf-8")
        return base_config_path


def _render_lane_runtime_config(
    symbol: str,
    strategy_name: str,
    strategy_source: str,
    *,
    manual_entry_exit_only: bool = False,
    fixed_notional_eur: float | None = None,
    exec_enabled: bool = True,
    md_interval_seconds: int | None = None,
    exec_reconcile_interval_sec: float | None = None,
) -> tuple[str, str]:
    base_config_path = _ensure_lane_base_config(symbol)
    runtime_config_path = _lane_runtime_config_path(symbol)
    base_cfg = _load_raw_yaml_config(base_config_path)
    runtime_cfg, alpha_type = build_rotation_runtime_config(base_cfg, strategy_name)
    risk = runtime_cfg.setdefault("risk", {})
    if isinstance(risk, dict):
        risk["manual_entry_exit_only"] = bool(manual_entry_exit_only)
    fixed_notional = max(0.0, float(fixed_notional_eur or 0.0))
    if fixed_notional > 0.0:
        order = runtime_cfg.setdefault("order", {})
        if isinstance(risk, dict):
            risk["max_exposure_mode"] = "fixed"
            risk["max_exposure_eur"] = fixed_notional
        if isinstance(order, dict):
            order["cycle_trade_mode"] = "fixed"
            order["cycle_trade_eur"] = fixed_notional
    exec_cfg = runtime_cfg.setdefault("exec", {})
    if isinstance(exec_cfg, dict):
        exec_cfg["enabled"] = bool(exec_enabled)
        if exec_reconcile_interval_sec is not None:
            exec_cfg["reconcile_interval_sec"] = float(exec_reconcile_interval_sec)
    md_cfg = runtime_cfg.setdefault("md", {})
    if isinstance(md_cfg, dict) and md_interval_seconds is not None:
        md_cfg["interval_seconds"] = int(md_interval_seconds)
    alpha_cfg = runtime_cfg.get("alpha")
    if isinstance(alpha_cfg, dict) and md_interval_seconds is not None:
        continuation_cfg = alpha_cfg.get("continuation")
        if isinstance(continuation_cfg, dict):
            continuation_cfg["bar_seconds"] = int(md_interval_seconds)
    runtime = runtime_cfg.setdefault("runtime", {})
    runtime["base_config_path"] = str(base_config_path)
    runtime["generated_config_path"] = str(runtime_config_path)
    runtime["rotation_strategy_source"] = str(strategy_source or "base_config")
    runtime["manual_entry_exit_only"] = bool(manual_entry_exit_only)

    # Rotation lanes can be quiet for longer periods under high system load.
    # Raise watchdog floors and cap IPC queue sizes to reduce restart storms
    # and memory pressure when many lanes run in parallel.
    ipc = runtime_cfg.setdefault("ipc", {})
    if isinstance(ipc, dict):
        try:
            heartbeat_timeout = float(ipc.get("heartbeat_timeout", 0.0) or 0.0)
        except Exception:
            heartbeat_timeout = 0.0
        if heartbeat_timeout < WATCHDOG_MIN_HEARTBEAT_TIMEOUT_SEC:
            ipc["heartbeat_timeout"] = WATCHDOG_MIN_HEARTBEAT_TIMEOUT_SEC
        try:
            startup_grace = float(ipc.get("startup_grace_sec", 0.0) or 0.0)
        except Exception:
            startup_grace = 0.0
        if startup_grace < WATCHDOG_MIN_STARTUP_GRACE_SEC:
            ipc["startup_grace_sec"] = WATCHDOG_MIN_STARTUP_GRACE_SEC

        queue_caps = {
            "journal_queue_size": IPC_MAX_JOURNAL_QUEUE_SIZE,
            "telemetry_queue_size": IPC_MAX_TELEMETRY_QUEUE_SIZE,
            "heartbeat_queue_size": IPC_MAX_HEARTBEAT_QUEUE_SIZE,
        }
        for key, cap in queue_caps.items():
            try:
                current = int(float(ipc.get(key, cap) or cap))
            except Exception:
                current = int(cap)
            if current <= 0 or current > cap:
                ipc[key] = int(cap)
    rendered = yaml.safe_dump(runtime_cfg, sort_keys=False)
    return alpha_type, rendered


def _write_lane_runtime_config(
    symbol: str,
    strategy_name: str,
    strategy_source: str,
    *,
    manual_entry_exit_only: bool = False,
    fixed_notional_eur: float | None = None,
    exec_enabled: bool = True,
    md_interval_seconds: int | None = None,
    exec_reconcile_interval_sec: float | None = None,
) -> tuple[str, bool]:
    runtime_config_path = _lane_runtime_config_path(symbol)
    alpha_type, rendered = _render_lane_runtime_config(
        symbol,
        strategy_name,
        strategy_source,
        manual_entry_exit_only=manual_entry_exit_only,
        fixed_notional_eur=fixed_notional_eur,
        exec_enabled=exec_enabled,
        md_interval_seconds=md_interval_seconds,
        exec_reconcile_interval_sec=exec_reconcile_interval_sec,
    )
    previous = ""
    try:
        previous = runtime_config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        previous = ""
    runtime_config_path.parent.mkdir(parents=True, exist_ok=True)
    if previous != rendered:
        runtime_config_path.write_text(rendered, encoding="utf-8")
        return alpha_type, True
    return alpha_type, False


def _manual_entry_exit_only_from_rendered(rendered: str) -> bool:
    text = str(rendered or "").strip()
    if not text:
        return False
    try:
        payload = yaml.safe_load(text)
    except Exception:
        return False
    if not isinstance(payload, dict):
        return False
    risk = payload.get("risk")
    if not isinstance(risk, dict):
        return False
    value = risk.get("manual_entry_exit_only")
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y"}
    return False


def _exec_enabled_from_rendered(rendered: str) -> bool:
    text = str(rendered or "").strip()
    if not text:
        return True
    try:
        payload = yaml.safe_load(text)
    except Exception:
        return True
    if not isinstance(payload, dict):
        return True
    exec_cfg = payload.get("exec")
    if not isinstance(exec_cfg, dict):
        return True
    value = exec_cfg.get("enabled")
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y"}
    return True


def _load_lane_runtime_metadata(symbol: str) -> tuple[str, str]:
    runtime_config_path = _lane_runtime_config_path(symbol)
    try:
        runtime_cfg = _load_raw_yaml_config(runtime_config_path)
    except Exception:
        return "", ""
    runtime = runtime_cfg.get("runtime")
    alpha_cfg = runtime_cfg.get("alpha")
    runtime_strategy = ""
    if isinstance(runtime, dict):
        runtime_strategy = normalize_rotation_strategy_name(runtime.get("rotation_strategy_name"))
    alpha_type = ""
    if isinstance(runtime, dict):
        alpha_type = str(runtime.get("rotation_alpha_type") or "").strip().lower()
    if not alpha_type and isinstance(alpha_cfg, dict):
        alpha_type = str(alpha_cfg.get("type") or "").strip().lower()
    return runtime_strategy, alpha_type


def _normalize_unit_config_path(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    if os.path.isabs(text):
        return os.path.abspath(text)
    return os.path.abspath(REPO_ROOT / text)


def _unit_environment(unit: str) -> dict[str, str]:
    try:
        out = subprocess.check_output(
            ["systemctl", "--user", "show", unit, "--property=Environment"],
            cwd=REPO_ROOT,
            text=True,
        )
    except Exception:
        return {}
    for line in out.splitlines():
        if not line.startswith("Environment="):
            continue
        payload = line.split("=", 1)[1].strip()
        if not payload:
            return {}
        env: dict[str, str] = {}
        for token in payload.split():
            if "=" not in token:
                continue
            key, value = token.split("=", 1)
            env[key] = value
        return env
    return {}


def _unit_env_int(unit: str, key: str) -> int:
    env = _unit_environment(unit)
    try:
        return int(str(env.get(key, "")).strip() or 0)
    except Exception:
        return 0


def _unit_env_path(unit: str, key: str) -> Path | None:
    env = _unit_environment(unit)
    raw = str(env.get(key, "")).strip()
    if not raw:
        return None
    try:
        return Path(_normalize_unit_config_path(raw))
    except Exception:
        return None


def _unit_main_pid(unit: str) -> int:
    try:
        return int(_unit_properties(unit, "MainPID").get("MainPID", "0") or 0)
    except Exception:
        return 0


def _path_markers(path: Path) -> tuple[set[str], set[str]]:
    absolute = os.path.abspath(path)
    raw = {absolute}
    try:
        rel = os.path.relpath(absolute, REPO_ROOT)
        if rel and rel != "." and not rel.startswith(f"..{os.sep}"):
            raw.add(rel)
    except Exception:
        pass
    return raw, {absolute}


def _env_matches_expected_path(text: str, expected_raw: set[str], expected_abs: set[str]) -> bool:
    candidate = str(text or "").strip()
    if not candidate:
        return False
    if candidate in expected_raw:
        return True
    try:
        return _normalize_unit_config_path(candidate) in expected_abs
    except Exception:
        return False


def _proc_environ(pid: int) -> dict[str, str]:
    try:
        payload = (PROC_ROOT / str(pid) / "environ").read_bytes()
    except Exception:
        return {}
    env: dict[str, str] = {}
    for token in payload.split(b"\0"):
        if not token or b"=" not in token:
            continue
        key, value = token.split(b"=", 1)
        try:
            env[key.decode("utf-8")] = value.decode("utf-8")
        except Exception:
            continue
    return env


def _proc_ppid_map() -> dict[int, int]:
    mapping: dict[int, int] = {}
    try:
        entries = list(PROC_ROOT.iterdir())
    except Exception:
        return mapping
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            status = (entry / "status").read_text(encoding="utf-8")
        except Exception:
            continue
        ppid = 0
        for line in status.splitlines():
            if line.startswith("PPid:"):
                try:
                    ppid = int(line.split(":", 1)[1].strip() or 0)
                except Exception:
                    ppid = 0
                break
        mapping[pid] = ppid
    return mapping


def _collect_descendant_pids(root_pid: int) -> set[int]:
    if root_pid <= 0:
        return set()
    children: dict[int, set[int]] = {}
    for pid, ppid in _proc_ppid_map().items():
        children.setdefault(ppid, set()).add(pid)
    seen: set[int] = set()
    stack = [root_pid]
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        stack.extend(children.get(pid, ()))
    return seen


def _lane_process_pids(symbol: str) -> set[int]:
    slug = str(LANES[symbol]["slug"])
    expected_raw: dict[str, set[str]] = {}
    expected_abs: dict[str, set[str]] = {}

    def _add_expected(key: str, path: Path) -> None:
        raw, abs_values = _path_markers(path)
        expected_raw.setdefault(key, set()).update(raw)
        expected_abs.setdefault(key, set()).update(abs_values)

    _add_expected("PID_FILE", REPO_ROOT / "logs" / f"{slug}_rotation_guard.pid")
    _add_expected("CHILD_PID_FILE", REPO_ROOT / "logs" / f"{slug}_rotation_guard.child.pid")
    _add_expected("DISABLE_FILE", REPO_ROOT / "logs" / f"{slug}_rotation_guard.disabled")
    _add_expected("GUARD_LOG", REPO_ROOT / "logs" / f"{slug}_rotation_guard.log")
    _add_expected("CONFIG", _lane_base_config_path(symbol))
    _add_expected("CONFIG", _lane_runtime_config_path(symbol))

    matched: set[int] = set()
    try:
        entries = list(PROC_ROOT.iterdir())
    except Exception:
        return matched
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        env = _proc_environ(pid)
        if not env or str(env.get("MODE", "")).strip().lower() != "live":
            continue
        for key in ("PID_FILE", "CHILD_PID_FILE", "DISABLE_FILE", "GUARD_LOG", "CONFIG"):
            if _env_matches_expected_path(
                env.get(key, ""),
                expected_raw.get(key, set()),
                expected_abs.get(key, set()),
            ):
                matched.add(pid)
                break
    return matched


def _cleanup_lane_orphan_processes(symbol: str, preserve_root_pid: int = 0) -> list[int]:
    preserve = _collect_descendant_pids(preserve_root_pid)
    stale = sorted(pid for pid in _lane_process_pids(symbol) if pid not in preserve)
    for pid in stale:
        _kill_pid(pid)
    return stale


def _lane_runtime_config_bootstrapped(symbol: str) -> bool:
    unit = _lane_unit_name(symbol)
    env = _unit_environment(unit)
    current = _normalize_unit_config_path(env.get("CONFIG", ""))
    if current != str(_lane_runtime_config_path(symbol)):
        return False
    for key, expected in LANE_RUNTIME_REQUIRED_ENV.items():
        if str(env.get(key, "")).strip() != expected:
            return False
    return True


def _http_ok(port: int, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def _post_control(port: int, path: str, timeout: float = 1.0) -> bool:
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(getattr(resp, "status", 0) or 0) == 200
    except Exception:
        return False


def _lane_exec_balances(port: int, timeout: float = 0.8) -> dict[str, float]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        exec_data = ((payload.get("data") or {}).get("exec") or {})
        balances = exec_data.get("balances") or {}
        if not isinstance(balances, dict):
            return {}
        out: dict[str, float] = {}
        for asset, row in balances.items():
            key = str(asset or "").strip().upper()
            if not key:
                continue
            try:
                if isinstance(row, dict):
                    qty = float(row.get("total") or row.get("free") or 0.0)
                else:
                    qty = float(row or 0.0)
            except Exception:
                qty = 0.0
            if abs(qty) > 0.0:
                out[key] = max(0.0, qty)
        return out
    except Exception:
        return {}


def _estimate_total_value_quote(balances: dict[str, float], *, quote: str = "USDC") -> float:
    if not isinstance(balances, dict) or not balances:
        return 0.0
    quote = str(quote or "USDC").upper()
    rest = BinanceRestClient(
        api_key="",
        api_secret="",
        symbol=f"BTC{quote}",
        base_url=str(os.environ.get("BINANCE_BASE_URL", "https://api.binance.com") or "https://api.binance.com"),
        timeout=2.0,
        max_retries=1,
    )
    stable_near_quote = {"USDT", "FDUSD", "BUSD", "TUSD", "USD", "USDS", "USDP"}
    total_quote = 0.0
    for asset, qty_raw in balances.items():
        qty = max(0.0, float(qty_raw or 0.0))
        if qty <= 0.0:
            continue
        asset_u = str(asset or "").upper()
        if not asset_u:
            continue
        if asset_u == quote:
            total_quote += qty
            continue
        rate = rest._asset_to_quote_rate(asset=asset_u, quote=quote)
        if rate <= 0.0 and quote == "USDC" and asset_u in stable_near_quote:
            rate = 1.0
        if rate <= 0.0:
            continue
        total_quote += qty * rate
    return max(0.0, float(total_quote))


def _lane_snapshot(symbol: str, port: int, timeout: float = 0.8) -> dict[str, float | int | bool]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        core = ((payload.get("data") or {}).get("core") or {})
        exec_data = ((payload.get("data") or {}).get("exec") or {})
        aggregate = payload.get("aggregate") or {}
        balances = exec_data.get("balances") or {}
        base_balance = 0.0
        if isinstance(balances, dict):
            entry = balances.get(symbol)
            try:
                if isinstance(entry, dict):
                    base_balance = abs(float(entry.get("total") or entry.get("free") or 0.0))
                else:
                    base_balance = abs(float(entry or 0.0))
            except Exception:
                base_balance = 0.0
        mark_price = abs(float(core.get("mark_price") or 0.0))
        return {
            "position_value_eur": abs(float(core.get("position_value_eur") or 0.0)),
            "position_btc": abs(float(core.get("position_btc") or 0.0)),
            "open_orders_count": int(aggregate.get("open_orders_count") or 0),
            "trading_enabled": bool(aggregate.get("trading_enabled", core.get("trading_enabled", False))),
            "max_exposure_mode": str(core.get("max_exposure_mode") or "").strip().lower(),
            "cycle_trade_mode": str(core.get("cycle_trade_mode") or "").strip().lower(),
            "base_balance": base_balance,
            "mark_price": mark_price,
            "base_balance_notional_eur": base_balance * mark_price,
            "cash_eur": abs(float(core.get("cash_eur") or 0.0)),
            "max_exposure_eur": abs(float(core.get("max_exposure_eur") or 0.0)),
            "cycle_trade_eur": abs(float(core.get("cycle_trade_eur") or 0.0)),
        }
    except Exception:
        return {
            "position_value_eur": 0.0,
            "position_btc": 0.0,
            "open_orders_count": 0,
            "trading_enabled": False,
            "max_exposure_mode": "",
            "cycle_trade_mode": "",
            "base_balance": 0.0,
            "mark_price": 0.0,
            "base_balance_notional_eur": 0.0,
            "cash_eur": 0.0,
            "max_exposure_eur": 0.0,
            "cycle_trade_eur": 0.0,
        }


def _snapshot_position_notional(snapshot: dict[str, float | int | bool] | None) -> float:
    if not isinstance(snapshot, dict):
        return 0.0
    position_value = abs(float(snapshot.get("position_value_eur", 0.0) or 0.0))
    position_btc = abs(float(snapshot.get("position_btc", 0.0) or 0.0))
    mark_price = abs(float(snapshot.get("mark_price", 0.0) or 0.0))
    base_notional = abs(float(snapshot.get("base_balance_notional_eur", 0.0) or 0.0))
    return max(position_value, position_btc * mark_price, base_notional)


def _lane_has_active_inventory(snapshot: dict[str, float | int | bool] | None) -> bool:
    if not isinstance(snapshot, dict):
        return False
    position_notional = _snapshot_position_notional(snapshot)
    open_orders = int(snapshot.get("open_orders_count", 0) or 0)
    return position_notional > INVENTORY_PROTECT_MIN_NOTIONAL_EUR or open_orders > 0


def _lane_has_position_inventory(snapshot: dict[str, float | int | bool] | None) -> bool:
    if not isinstance(snapshot, dict):
        return False
    return _snapshot_position_notional(snapshot) > INVENTORY_PROTECT_MIN_NOTIONAL_EUR


def _should_defer_runtime_update(
    snapshot: dict[str, float | int | bool] | None,
    current_rendered: str,
    desired_rendered: str,
) -> bool:
    return _lane_has_position_inventory(snapshot) and current_rendered != desired_rendered


def _wait_lane_flattened(port: int, timeout: float = 6.0) -> bool:
    deadline = time.time() + max(0.1, timeout)
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=0.8) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            core = ((payload.get("data") or {}).get("core") or {})
            aggregate = payload.get("aggregate") or {}
            pos_val = abs(float(core.get("position_value_eur") or 0.0))
            pos_btc = abs(float(core.get("position_btc") or 0.0))
            open_orders = aggregate.get("open_orders_count")
            open_ok = (open_orders is None) or (int(open_orders) == 0)
            if pos_val <= 0.05 and pos_btc <= 1e-8 and open_ok:
                return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def _wait_lane_trading_enabled(port: int, enabled: bool, timeout: float = 4.0) -> bool:
    deadline = time.time() + max(0.1, timeout)
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=0.8) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            core = ((payload.get("data") or {}).get("core") or {})
            aggregate = payload.get("aggregate") or {}
            lane_enabled = bool(aggregate.get("trading_enabled", core.get("trading_enabled", False)))
            if lane_enabled is enabled:
                return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def _set_lane_watch_only(
    symbol: str,
    *,
    force_pause_with_inventory: bool = False,
    keep_trading_enabled: bool = False,
) -> bool:
    lane = LANES[symbol]
    control_port = lane["ports"][0]
    if not _http_ok(control_port, timeout=0.7):
        return False
    snap = _lane_snapshot(symbol, control_port)
    has_inventory = _lane_has_position_inventory(snap)
    open_orders = int(snap.get("open_orders_count", 0) or 0) if isinstance(snap, dict) else 0
    trading_enabled = bool(snap.get("trading_enabled", True)) if isinstance(snap, dict) else True
    if keep_trading_enabled:
        if open_orders > 0:
            _post_control(control_port, "/cancel_all", timeout=1.0)
        if not trading_enabled:
            _post_control(control_port, "/resume", timeout=1.0)
            _wait_lane_trading_enabled(control_port, enabled=True, timeout=4.0)
        return has_inventory and not force_pause_with_inventory
    if has_inventory and not force_pause_with_inventory:
        if not trading_enabled:
            _post_control(control_port, "/resume", timeout=1.0)
            _wait_lane_trading_enabled(control_port, enabled=True, timeout=4.0)
        return True
    if trading_enabled:
        _post_control(control_port, "/pause", timeout=1.0)
        _wait_lane_trading_enabled(control_port, enabled=False, timeout=4.0)
    if trading_enabled or open_orders > 0:
        _post_control(control_port, "/cancel_all", timeout=1.0)
    return has_inventory and not force_pause_with_inventory


def _load_manual_watch_symbols() -> list[str]:
    if not MANUAL_LANES_FILE.exists():
        return []
    try:
        payload = json.loads(MANUAL_LANES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []

    raw_symbols: list[str] = []
    if isinstance(payload, list):
        raw_symbols.extend(str(item) for item in payload)
    elif isinstance(payload, dict):
        for key in ("watch_symbols", "manual_watch_symbols", "symbols"):
            value = payload.get(key)
            if isinstance(value, list):
                raw_symbols.extend(str(item) for item in value)

    out: list[str] = []
    seen: set[str] = set()
    for item in raw_symbols:
        symbol = str(item or "").strip().upper()
        if not symbol or symbol not in LANES or symbol in seen:
            continue
        seen.add(symbol)
        out.append(symbol)
    return out


def _load_manual_entry_exit_only_config() -> tuple[bool, set[str]]:
    if not MANUAL_LANES_FILE.exists():
        return False, set()
    try:
        payload = json.loads(MANUAL_LANES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return False, set()
    if not isinstance(payload, dict):
        return False, set()

    raw_global = payload.get("manual_entry_exit_only")
    manual_global = False
    if isinstance(raw_global, bool):
        manual_global = raw_global
    elif isinstance(raw_global, (int, float)):
        manual_global = bool(raw_global)
    elif isinstance(raw_global, str):
        manual_global = raw_global.strip().lower() in {"1", "true", "yes", "on", "y"}

    symbols: set[str] = set()
    for key in (
        "manual_entry_exit_only_symbols",
        "manual_entry_symbols",
        "entry_exit_only_symbols",
    ):
        value = payload.get(key)
        if not isinstance(value, list):
            continue
        for item in value:
            symbol = str(item or "").strip().upper()
            if symbol and symbol in LANES:
                symbols.add(symbol)
    return manual_global, symbols


def _expected_runtime_notional(
    snapshot: dict[str, float | int | bool],
    *,
    expected_fraction: float,
    min_entry_notional_eur: float = 0.0,
) -> float:
    cash = max(0.0, float(snapshot.get("cash_eur", 0.0) or 0.0))
    position_value = max(0.0, float(snapshot.get("position_value_eur", 0.0) or 0.0))
    equity = max(cash, cash + position_value)
    fraction = max(0.0, min(1.0, float(expected_fraction)))
    expected = max(0.0, equity * fraction)
    min_entry = max(0.0, float(min_entry_notional_eur or 0.0))
    if expected > 0.0 and expected + 1e-9 < min_entry and equity + 1e-9 >= min_entry:
        return min_entry
    return expected


def _runtime_fraction_mismatch(
    symbol: str,
    snapshot: dict[str, float | int | bool],
    expected_fraction: float,
    *,
    expected_notional: float | None = None,
) -> bool:
    max_exposure = max(0.0, float(snapshot.get("max_exposure_eur", 0.0) or 0.0))
    cycle_trade = max(0.0, float(snapshot.get("cycle_trade_eur", 0.0) or 0.0))
    if expected_notional is None:
        expected = _expected_runtime_notional(
            snapshot,
            expected_fraction=expected_fraction,
            min_entry_notional_eur=_configured_min_entry_notional(symbol),
        )
    else:
        expected = max(0.0, float(expected_notional or 0.0))
    if expected <= 0.0:
        return max_exposure > 0.25 or cycle_trade > 0.25
    tolerance = max(0.5, expected * 0.15)
    return abs(max_exposure - expected) > tolerance or abs(cycle_trade - expected) > tolerance


def _set_lane_runtime_fixed_budget(symbol: str, notional_eur: float) -> bool:
    runtime_config_path = _lane_runtime_config_path(symbol)
    if not runtime_config_path.exists():
        return False
    try:
        with runtime_config_path.open("r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle)
    except Exception:
        return False
    if not isinstance(payload, dict):
        return False
    risk = payload.setdefault("risk", {})
    order = payload.setdefault("order", {})
    if not isinstance(risk, dict) or not isinstance(order, dict):
        return False

    target = max(0.0, float(notional_eur or 0.0))
    changed = False

    def _set_if_diff(section: dict, key: str, value: object) -> None:
        nonlocal changed
        if section.get(key) != value:
            section[key] = value
            changed = True

    _set_if_diff(risk, "max_exposure_mode", "fixed")
    _set_if_diff(order, "cycle_trade_mode", "fixed")
    _set_if_diff(risk, "max_exposure_eur", target)
    _set_if_diff(order, "cycle_trade_eur", target)

    if not changed:
        return False
    rendered = yaml.safe_dump(payload, sort_keys=False)
    runtime_config_path.write_text(rendered, encoding="utf-8")
    return True


def _set_lane_active(
    symbol: str,
    expected_fraction: float,
    *,
    expected_notional: float | None = None,
) -> None:
    lane = LANES[symbol]
    control_port = lane["ports"][0]
    if not _http_ok(control_port, timeout=0.7):
        return
    snap = _lane_snapshot(symbol, control_port)
    if _runtime_fraction_mismatch(
        symbol,
        snap,
        expected_fraction,
        expected_notional=expected_notional,
    ):
        if not _lane_has_position_inventory(snap):
            _restart_lane_runtime(symbol)
            control_port = lane["ports"][0]
            snap = _lane_snapshot(symbol, control_port)
    mark_price = abs(float(snap.get("mark_price", 0.0) or 0.0))
    has_position = max(
        float(snap.get("position_value_eur", 0.0) or 0.0),
        abs(float(snap.get("position_btc", 0.0) or 0.0)) * mark_price,
    ) > 0.05
    open_orders = int(snap.get("open_orders_count", 0) or 0)
    if not has_position and open_orders > 1:
        _post_control(control_port, "/cancel_all", timeout=1.0)
    _post_control(control_port, "/resume", timeout=1.0)
    _wait_lane_trading_enabled(control_port, enabled=True, timeout=4.0)


def _compute_selected_shared_equity(selected: list[str]) -> tuple[float, dict[str, dict[str, float | int | bool]]]:
    snapshots: dict[str, dict[str, float | int | bool]] = {}
    if not selected:
        return 0.0, snapshots
    shared_cash = 0.0
    total_selected_positions = 0.0
    for symbol in selected:
        lane = LANES[symbol]
        control_port = int(lane["ports"][0])
        snap = _lane_snapshot(symbol, control_port) if _http_ok(control_port, timeout=0.5) else {}
        snapshots[symbol] = snap
        shared_cash = max(shared_cash, abs(float(snap.get("cash_eur", 0.0) or 0.0)))
        total_selected_positions += _snapshot_position_notional(snap)
    fallback_total = max(0.0, shared_cash + total_selected_positions)

    balances: dict[str, float] = {}
    for symbol in selected:
        lane = LANES[symbol]
        control_port = int(lane["ports"][0])
        if not _http_ok(control_port, timeout=0.5):
            continue
        balances = _lane_exec_balances(control_port, timeout=0.8)
        if balances:
            break
    if balances:
        estimated_total = _estimate_total_value_quote(balances, quote="USDC")
        if estimated_total > 0.0:
            return estimated_total, snapshots
    return fallback_total, snapshots


def _unit_properties(unit: str, *properties: str) -> dict[str, str]:
    property_arg = ",".join(properties) if properties else "LoadState,ActiveState,SubState"
    try:
        out = subprocess.check_output(
            ["systemctl", "--user", "show", unit, f"--property={property_arg}"],
            cwd=REPO_ROOT,
            text=True,
        )
    except Exception:
        return {}
    values: dict[str, str] = {}
    for line in out.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def _unit_state(unit: str) -> tuple[str, str]:
    values = _unit_properties(unit, "ActiveState", "SubState")
    return values.get("ActiveState", ""), values.get("SubState", "")


def _unit_load_state(unit: str) -> str:
    return _unit_properties(unit, "LoadState").get("LoadState", "")


def _wait_unit_released(unit: str, timeout: float = LANE_UNIT_TIMEOUT_SEC) -> bool:
    deadline = time.time() + max(0.1, timeout)
    while time.time() < deadline:
        values = _unit_properties(unit, "LoadState", "ActiveState", "SubState")
        load_state = values.get("LoadState", "")
        active_state = values.get("ActiveState", "")
        sub_state = values.get("SubState", "")
        if load_state in {"", "not-found"}:
            return True
        if active_state in {"inactive", "failed", ""} and sub_state in {"dead", "failed", "", "exited"}:
            if load_state not in {"loaded", "merged", "stub"}:
                return True
        time.sleep(0.25)
    return False


def _wait_control_ready(control_port: int, timeout: float = 25.0) -> bool:
    deadline = time.time() + max(0.1, timeout)
    while time.time() < deadline:
        if _http_ok(control_port, timeout=0.5):
            return True
        time.sleep(1.0)
    return False


def _kill_pid(pid: int, *, graceful: bool = True) -> None:
    if pid <= 0:
        return
    signals: tuple[signal.Signals, ...]
    if graceful:
        signals = (signal.SIGINT, signal.SIGTERM, signal.SIGKILL)
    else:
        signals = (signal.SIGKILL,)
    for sig in signals:
        try:
            os.kill(pid, sig)
        except Exception:
            return
        for _ in range(10):
            try:
                os.kill(pid, 0)
            except Exception:
                return
            time.sleep(0.2)


def _stop_lane(symbol: str) -> None:
    lane = LANES[symbol]
    slug = lane["slug"]
    unit = _lane_unit_name(symbol)
    control_port = lane["ports"][0]
    disable_file = REPO_ROOT / "logs" / f"{slug}_rotation_guard.disabled"
    pid_file = REPO_ROOT / "logs" / f"{slug}_rotation_guard.pid"
    child_pid_file = REPO_ROOT / "logs" / f"{slug}_rotation_guard.child.pid"

    disable_file.write_text("", encoding="utf-8")

    # For automatic lane rotation we do not force market exits.
    # Keep the lane quiet (pause + cancel_all), then stop processes below.
    if _http_ok(control_port, timeout=0.7):
        _post_control(control_port, "/pause", timeout=1.0)
        _post_control(control_port, "/cancel_all", timeout=1.0)

    for path in (child_pid_file, pid_file):
        try:
            pid = int(path.read_text(encoding="utf-8").strip())
        except Exception:
            pid = 0
        _kill_pid(pid, graceful=False)

    subprocess.run(
        ["systemctl", "--user", "kill", "--kill-who=all", "--signal=SIGKILL", unit],
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    deadline = time.time() + LANE_UNIT_TIMEOUT_SEC
    while time.time() < deadline:
        active_state, sub_state = _unit_state(unit)
        if active_state in {"inactive", "failed", ""}:
            break
        if sub_state == "dead":
            break
        time.sleep(0.2)
    subprocess.run(
        ["systemctl", "--user", "reset-failed", unit],
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )

    for path in (pid_file, child_pid_file):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _stop_unmanaged_lane(symbol: str) -> None:
    slug = str(symbol or "").strip().lower()
    if not slug:
        return
    unit = f"codex-rotation-{slug}.service"
    control_port = _unit_env_int(unit, "CONTROL_PORT")
    disable_file = _unit_env_path(unit, "DISABLE_FILE") or (REPO_ROOT / "logs" / f"{slug}_rotation_guard.disabled")
    pid_file = _unit_env_path(unit, "PID_FILE") or (REPO_ROOT / "logs" / f"{slug}_rotation_guard.pid")
    child_pid_file = _unit_env_path(unit, "CHILD_PID_FILE") or (REPO_ROOT / "logs" / f"{slug}_rotation_guard.child.pid")

    disable_file.parent.mkdir(parents=True, exist_ok=True)
    disable_file.write_text("", encoding="utf-8")

    if control_port > 0 and _http_ok(control_port, timeout=0.7):
        _post_control(control_port, "/pause", timeout=1.0)
        _post_control(control_port, "/cancel_all", timeout=1.0)

    for path in (child_pid_file, pid_file):
        try:
            pid = int(path.read_text(encoding="utf-8").strip())
        except Exception:
            pid = 0
        _kill_pid(pid, graceful=False)

    subprocess.run(
        ["systemctl", "--user", "kill", "--kill-who=all", "--signal=SIGKILL", unit],
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    deadline = time.time() + LANE_UNIT_TIMEOUT_SEC
    while time.time() < deadline:
        active_state, sub_state = _unit_state(unit)
        if active_state in {"inactive", "failed", ""}:
            break
        if sub_state == "dead":
            break
        time.sleep(0.2)
    subprocess.run(
        ["systemctl", "--user", "reset-failed", unit],
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )

    for path in (pid_file, child_pid_file):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _start_lane(symbol: str, *, force_recreate: bool = False) -> None:
    lane = LANES[symbol]
    slug = lane["slug"]
    unit = f"codex-rotation-{slug}"
    control_port, exec_port, journal_port, core_port, md_port = lane["ports"]
    disable_file = REPO_ROOT / "logs" / f"{slug}_rotation_guard.disabled"
    pid_file = REPO_ROOT / "logs" / f"{slug}_rotation_guard.pid"
    child_pid_file = REPO_ROOT / "logs" / f"{slug}_rotation_guard.child.pid"
    guard_log = REPO_ROOT / "logs" / f"{slug}_rotation_guard.log"
    runtime_config_path = _lane_runtime_config_path(symbol)

    for path in (disable_file, pid_file, child_pid_file):
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    unit_service = f"{unit}.service"
    active_state, sub_state = _unit_state(unit_service)
    if not force_recreate and (active_state or sub_state) and _lane_runtime_config_bootstrapped(symbol):
        subprocess.run(
            ["systemctl", "--user", "restart", unit_service],
            cwd=REPO_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if _wait_control_ready(control_port, timeout=25.0):
            return

    subprocess.run(
        ["systemctl", "--user", "stop", unit_service],
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    subprocess.run(
        ["systemctl", "--user", "reset-failed", unit_service],
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    _wait_unit_released(unit_service, timeout=LANE_UNIT_TIMEOUT_SEC)

    run_cmd = "./scripts/live_guard.sh >> \"$GUARD_LOG\" 2>&1"
    run_command = [
        "systemd-run",
        "--user",
        "--quiet",
        "--unit",
        unit,
        f"--property=WorkingDirectory={REPO_ROOT}",
        f"--property=TimeoutStopSec={int(LANE_UNIT_TIMEOUT_SEC)}s",
        "--property=KillMode=mixed",
        "--setenv=MODE=live",
        f"--setenv=CONFIG={runtime_config_path}",
        "--setenv=START_IMPACT_CONSOLE=0",
        "--setenv=START_JOURNAL_GUI=0",
        "--setenv=START_CORE_GUI=0",
        "--setenv=START_MD_GUI=0",
        "--setenv=START_EXEC=0",
        f"--setenv=CONTROL_PORT={control_port}",
        f"--setenv=EXEC_PORT={exec_port}",
        f"--setenv=JOURNAL_GUI_PORT={journal_port}",
        f"--setenv=CORE_GUI_PORT={core_port}",
        f"--setenv=MD_GUI_PORT={md_port}",
        f"--setenv=GUARD_LOG={guard_log}",
        f"--setenv=PID_FILE={pid_file}",
        f"--setenv=CHILD_PID_FILE={child_pid_file}",
        f"--setenv=DISABLE_FILE={disable_file}",
        "/bin/bash",
        "-lc",
        run_cmd,
    ]
    launch_error = ""
    launched = False
    try:
        subprocess.check_call(
            run_command,
            cwd=REPO_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        launched = True
    except subprocess.CalledProcessError as exc:
        launch_error = str(exc)
        if _unit_load_state(unit_service) == "loaded" and _lane_runtime_config_bootstrapped(symbol):
            subprocess.run(
                ["systemctl", "--user", "restart", unit_service],
                cwd=REPO_ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if _wait_control_ready(control_port, timeout=25.0):
                return

    if launched:
        if _wait_control_ready(control_port, timeout=25.0):
            return
        raise RuntimeError(f"{symbol} control did not become ready on {control_port}")

    for attempt in range(3):
        proc = subprocess.run(
            run_command,
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            launched = True
            break

        err_parts = []
        stdout_text = str(proc.stdout).strip() if proc.stdout is not None else ""
        stderr_text = str(proc.stderr).strip() if proc.stderr is not None else ""
        if stdout_text:
            err_parts.append(stdout_text)
        if stderr_text:
            err_parts.append(stderr_text)
        launch_error = " | ".join(part for part in err_parts if part) or f"systemd-run exit={proc.returncode}"
        err_norm = launch_error.lower()
        load_state = _unit_load_state(unit_service)
        is_loaded_conflict = (
            "already loaded or has a fragment file" in err_norm
            or "already loaded" in err_norm
            or "fragment file" in err_norm
        )

        # If the unit is already loaded, prefer reusing/restarting the existing
        # lane first to avoid failing the selector cycle on transient races.
        if load_state == "loaded" and _lane_runtime_config_bootstrapped(symbol):
            subprocess.run(
                ["systemctl", "--user", "restart", unit_service],
                cwd=REPO_ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if _wait_control_ready(control_port, timeout=25.0):
                return

        if is_loaded_conflict or load_state == "loaded":
            subprocess.run(
                ["systemctl", "--user", "stop", "--no-block", unit_service],
                cwd=REPO_ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            subprocess.run(
                ["systemctl", "--user", "kill", "--kill-who=all", "--signal=SIGKILL", unit_service],
                cwd=REPO_ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            subprocess.run(
                ["systemctl", "--user", "reset-failed", unit_service],
                cwd=REPO_ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            _wait_unit_released(unit_service, timeout=LANE_UNIT_TIMEOUT_SEC + (attempt * 2.0))
            continue

        if not _wait_unit_released(unit_service, timeout=max(4.0, LANE_UNIT_TIMEOUT_SEC / 2.0)):
            break

    if not launched:
        raise RuntimeError(f"{symbol} failed to launch lane unit: {launch_error}")

    if _wait_control_ready(control_port, timeout=25.0):
        return
    raise RuntimeError(f"{symbol} control did not become ready on {control_port}")


def _restart_lane_runtime(symbol: str) -> None:
    lane = LANES[symbol]
    unit = _lane_unit_name(symbol)
    control_port = lane["ports"][0]
    subprocess.run(
        ["systemctl", "--user", "restart", unit],
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    deadline = time.time() + 25.0
    while time.time() < deadline:
        if _http_ok(control_port, timeout=0.5):
            return
        time.sleep(1.0)
    raise RuntimeError(f"{symbol} control did not come back after restart on {control_port}")


def _reload_lane(symbol: str) -> None:
    lane = LANES[symbol]
    control_port = lane["ports"][0]
    if not _http_ok(control_port, timeout=0.5):
        return
    _post_control(control_port, "/reload", timeout=1.0)


def _pause_lane(symbol: str) -> None:
    lane = LANES[symbol]
    control_port = lane["ports"][0]
    if not _http_ok(control_port, timeout=0.7):
        return
    _post_control(control_port, "/pause", timeout=1.0)
    _wait_lane_trading_enabled(control_port, enabled=False, timeout=4.0)


def _cleanup_orphans_all_lanes() -> list[dict[str, object]]:
    cleanup: list[dict[str, object]] = []
    for symbol in sorted(LANES):
        unit_service = _lane_unit_name(symbol)
        unit_main_pid = _unit_main_pid(unit_service)
        removed = _cleanup_lane_orphan_processes(symbol, preserve_root_pid=unit_main_pid)
        if removed:
            cleanup.append({"symbol": symbol, "killed_pids": removed})
    return cleanup


def main() -> None:
    ap = argparse.ArgumentParser(description="Apply the active/watch rotation state to lane services")
    ap.add_argument(
        "--reload-running",
        action="store_true",
        help="Reload already running watch lanes before applying active/watch state",
    )
    ap.add_argument(
        "--cleanup-orphans-only",
        action="store_true",
        help="Only clean orphan lane processes and exit",
    )
    args = ap.parse_args()

    if args.cleanup_orphans_only:
        cleanup = _cleanup_orphans_all_lanes()
        print(
            json.dumps(
                {
                    "ok": True,
                    "mode": "cleanup_orphans_only",
                    "orphan_process_cleanup": cleanup,
                },
                ensure_ascii=True,
            )
        )
        return

    if not ACTIVE_FILE.exists():
        raise SystemExit("missing configs/rotation_active_lanes.json")
    state = json.loads(ACTIVE_FILE.read_text(encoding="utf-8"))
    profile_values = state.get("profile_values") if isinstance(state.get("profile_values"), dict) else {}
    try:
        state_md_interval_seconds = max(1, int(float(profile_values.get("md_interval_seconds", 60) or 60)))
    except Exception:
        state_md_interval_seconds = 60
    try:
        state_exec_reconcile_interval_sec = max(
            1.0,
            float(profile_values.get("exec_reconcile_interval_sec", 5.0) or 5.0),
        )
    except Exception:
        state_exec_reconcile_interval_sec = 5.0
    selected = [str(x).upper() for x in state.get("selected", []) if str(x).upper() in LANES]
    watch_symbols = [str(x).upper() for x in state.get("watch_symbols", selected) if str(x).upper() in LANES]
    if not watch_symbols:
        watch_symbols = list(selected)
    manual_watch_symbols = _load_manual_watch_symbols()
    manual_watch_set = set(manual_watch_symbols)
    manual_entry_exit_only_global, manual_entry_exit_only_symbols = _load_manual_entry_exit_only_config()
    for symbol in manual_watch_symbols:
        if symbol not in watch_symbols:
            watch_symbols.append(symbol)
    selected_set = set(selected)
    inventory_protected_watch_symbols: set[str] = set()
    for symbol in POOL:
        if symbol in watch_symbols:
            continue
        control_port = int(LANES[symbol]["ports"][0])
        if not _http_ok(control_port, timeout=0.5):
            continue
        snap = _lane_snapshot(symbol, control_port)
        if _lane_has_position_inventory(snap):
            watch_symbols.append(symbol)
            inventory_protected_watch_symbols.add(symbol)
    selected_strategy_map = normalize_symbol_strategy_map(state.get("selected_strategy_map"))
    row_strategy_map = _state_row_strategy_map(state)
    state_fraction = max(0.0, min(1.0, float(state.get("fraction", 0.0) or 0.0)))
    selected_fraction_map_raw = state.get("selected_fraction_map") or {}
    selected_fraction_map: dict[str, float] = {}
    if isinstance(selected_fraction_map_raw, dict):
        for key, value in selected_fraction_map_raw.items():
            try:
                selected_fraction_map[str(key).upper()] = max(0.0, min(1.0, float(value or 0.0)))
            except Exception:
                continue
    # Compute per-selected-lane fixed budget once and render it directly into
    # runtime configs to avoid an intermediate equity-mode phase.
    selected_shared_equity, _selected_snapshots = _compute_selected_shared_equity(selected)
    selected_expected_notional_map: dict[str, float] = {}
    for symbol in selected:
        fraction = max(0.0, min(1.0, float(selected_fraction_map.get(symbol, state_fraction) or 0.0)))
        selected_expected_notional_map[symbol] = max(0.0, selected_shared_equity * fraction)
    watch_strategy_map: dict[str, str] = {}
    watch_alpha_map: dict[str, str] = {}
    runtime_config_changed: dict[str, bool] = {}
    runtime_budget_overrides: list[dict[str, float]] = []
    manual_entry_exit_only_by_symbol: dict[str, bool] = {}
    exec_enabled_by_symbol: dict[str, bool] = {}
    deferred_runtime_updates: list[dict[str, str]] = []
    deferred_runtime_symbols: set[str] = set()
    restart_required_symbols: set[str] = set()
    deferred_watch_only_symbols: list[dict[str, str]] = []
    managed_stopped_symbols: list[str] = []
    unmanaged_stopped_symbols: list[str] = []
    unmanaged_inventory_protected_symbols: list[str] = []
    for symbol in watch_symbols:
        # Non-selected lanes must never re-enter automatically; keep them exit-only.
        manual_entry_exit_only = bool(
            manual_entry_exit_only_global
            or symbol not in selected_set
            or symbol in manual_watch_set
            or symbol in manual_entry_exit_only_symbols
        )
        manual_entry_exit_only_by_symbol[symbol] = manual_entry_exit_only
        strategy_name, strategy_source = _resolve_lane_strategy(
            symbol,
            selected_strategy_map=selected_strategy_map,
            row_strategy_map=row_strategy_map,
        )
        fixed_notional = (
            max(0.0, float(selected_expected_notional_map.get(symbol, 0.0) or 0.0))
            if symbol in selected_set
            else 0.0
        )
        control_port = LANES[symbol]["ports"][0]
        lane_running = _http_ok(control_port, timeout=0.5)
        snap = _lane_snapshot(symbol, control_port) if lane_running else None
        has_inventory = _lane_has_position_inventory(snap) if isinstance(snap, dict) else False
        exec_enabled = bool(
            symbol in selected_set
            or symbol in manual_watch_set
            or symbol in manual_entry_exit_only_symbols
            or has_inventory
        )
        exec_enabled_by_symbol[symbol] = exec_enabled
        alpha_type, desired_rendered = _render_lane_runtime_config(
            symbol,
            strategy_name,
            strategy_source,
            manual_entry_exit_only=manual_entry_exit_only,
            fixed_notional_eur=fixed_notional,
            exec_enabled=exec_enabled,
            md_interval_seconds=state_md_interval_seconds,
            exec_reconcile_interval_sec=state_exec_reconcile_interval_sec,
        )
        runtime_config_path = _lane_runtime_config_path(symbol)
        try:
            current_rendered = runtime_config_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            current_rendered = ""
        manual_flag_changed = (
            _manual_entry_exit_only_from_rendered(current_rendered)
            != _manual_entry_exit_only_from_rendered(desired_rendered)
        )
        exec_flag_changed = (
            _exec_enabled_from_rendered(current_rendered)
            != _exec_enabled_from_rendered(desired_rendered)
        )
        if lane_running and _should_defer_runtime_update(snap, current_rendered, desired_rendered):
            live_strategy, live_alpha = _load_lane_runtime_metadata(symbol)
            deferred_runtime_symbols.add(symbol)
            deferred_runtime_updates.append(
                {
                    "symbol": symbol,
                    "current_strategy": live_strategy or "unknown",
                    "current_alpha": live_alpha or "unknown",
                    "desired_strategy": strategy_name or "base_config",
                    "desired_alpha": alpha_type or "unknown",
                }
            )
            watch_strategy_map[symbol] = strategy_name
            watch_alpha_map[symbol] = alpha_type
            runtime_config_changed[symbol] = False
            continue
        if exec_flag_changed:
            restart_required_symbols.add(symbol)
        alpha_type, changed = _write_lane_runtime_config(
            symbol,
            strategy_name,
            strategy_source,
            manual_entry_exit_only=manual_entry_exit_only,
            fixed_notional_eur=fixed_notional,
            exec_enabled=exec_enabled,
            md_interval_seconds=state_md_interval_seconds,
            exec_reconcile_interval_sec=state_exec_reconcile_interval_sec,
        )
        if symbol in selected_set and fixed_notional > 0.0 and changed:
            runtime_budget_overrides.append({"symbol": symbol, "notional_eur": fixed_notional})
        watch_strategy_map[symbol] = strategy_name
        watch_alpha_map[symbol] = alpha_type
        runtime_config_changed[symbol] = changed

    for symbol in POOL:
        if symbol in watch_symbols:
            continue
        control_port = int(LANES[symbol]["ports"][0])
        unit_service = _lane_unit_name(symbol)
        lane_running = _http_ok(control_port, timeout=0.5)
        active_state, sub_state = _unit_state(unit_service)
        if not lane_running and active_state in {"", "inactive"} and sub_state in {"", "dead", "failed", "exited"}:
            continue
        _stop_lane(symbol)
        managed_stopped_symbols.append(symbol)
        _cleanup_lane_orphan_processes(symbol)

    managed_watch_set = set(watch_symbols)
    for symbol in list_running_rotation_symbols():
        if symbol in managed_watch_set or symbol in LANES:
            continue
        unit_service = f"codex-rotation-{symbol.lower()}.service"
        control_port = _unit_env_int(unit_service, "CONTROL_PORT")
        lane_running = control_port > 0 and _http_ok(control_port, timeout=0.5)
        active_state, sub_state = _unit_state(unit_service)
        if not lane_running and active_state in {"", "inactive"} and sub_state in {"", "dead", "failed", "exited"}:
            continue
        snap = _lane_snapshot(symbol, control_port) if lane_running and control_port > 0 else None
        if _lane_has_active_inventory(snap):
            unmanaged_inventory_protected_symbols.append(symbol)
            continue
        _stop_unmanaged_lane(symbol)
        unmanaged_stopped_symbols.append(symbol)

    failed_start: list[dict[str, str]] = []
    orphan_cleanup: list[dict[str, object]] = []
    for symbol in watch_symbols:
        unit_service = _lane_unit_name(symbol)
        active_state, sub_state = _unit_state(unit_service)
        lane_running = _http_ok(LANES[symbol]["ports"][0], timeout=0.5)
        if lane_running and symbol in deferred_runtime_symbols:
            unit_main_pid = _unit_main_pid(unit_service)
            if unit_main_pid > 0:
                removed = _cleanup_lane_orphan_processes(symbol, preserve_root_pid=unit_main_pid)
                if removed:
                    orphan_cleanup.append({"symbol": symbol, "killed_pids": removed})
            continue
        bootstrapped = _lane_runtime_config_bootstrapped(symbol)
        if not bootstrapped and (
            lane_running
            or active_state not in {"", "inactive"}
            or sub_state not in {"", "dead"}
        ):
            try:
                _start_lane(symbol, force_recreate=True)
                if symbol in selected_set:
                    _pause_lane(symbol)
            except Exception as exc:
                failed_start.append({"symbol": symbol, "error": str(exc)})
                continue
            unit_main_pid = _unit_main_pid(unit_service)
            if unit_main_pid > 0:
                removed = _cleanup_lane_orphan_processes(symbol, preserve_root_pid=unit_main_pid)
                if removed:
                    orphan_cleanup.append({"symbol": symbol, "killed_pids": removed})
            continue
        if lane_running:
            if not bootstrapped:
                try:
                    _start_lane(symbol, force_recreate=True)
                    if symbol in selected_set:
                        _pause_lane(symbol)
                except Exception as exc:
                    failed_start.append({"symbol": symbol, "error": str(exc)})
                    continue
                unit_main_pid = _unit_main_pid(unit_service)
                if unit_main_pid > 0:
                    removed = _cleanup_lane_orphan_processes(symbol, preserve_root_pid=unit_main_pid)
                if removed:
                    orphan_cleanup.append({"symbol": symbol, "killed_pids": removed})
                continue
            if symbol in restart_required_symbols:
                _restart_lane_runtime(symbol)
            elif args.reload_running or runtime_config_changed.get(symbol, False):
                _reload_lane(symbol)
            unit_main_pid = _unit_main_pid(unit_service)
            if unit_main_pid > 0:
                removed = _cleanup_lane_orphan_processes(symbol, preserve_root_pid=unit_main_pid)
                if removed:
                    orphan_cleanup.append({"symbol": symbol, "killed_pids": removed})
            continue
        try:
            _start_lane(symbol)
            if symbol in selected_set:
                _pause_lane(symbol)
        except Exception as exc:
            failed_start.append({"symbol": symbol, "error": str(exc)})
            continue
        unit_main_pid = _unit_main_pid(unit_service)
        if unit_main_pid > 0:
            removed = _cleanup_lane_orphan_processes(symbol, preserve_root_pid=unit_main_pid)
            if removed:
                orphan_cleanup.append({"symbol": symbol, "killed_pids": removed})

    for symbol in watch_symbols:
        try:
            if symbol in selected_set:
                _set_lane_active(
                    symbol,
                    selected_fraction_map.get(symbol, state_fraction),
                    expected_notional=selected_expected_notional_map.get(symbol),
                )
            else:
                if _set_lane_watch_only(
                    symbol,
                    force_pause_with_inventory=False,
                    keep_trading_enabled=bool(manual_entry_exit_only_by_symbol.get(symbol, False)),
                ):
                    live_strategy, live_alpha = _load_lane_runtime_metadata(symbol)
                    deferred_watch_only_symbols.append(
                        {
                            "symbol": symbol,
                            "current_strategy": live_strategy or "unknown",
                            "current_alpha": live_alpha or "unknown",
                            "reason": "inventory_open",
                        }
                    )
        except Exception as exc:
            failed_start.append({"symbol": symbol, "error": str(exc)})

    print(
        json.dumps(
            {
                "ok": len(failed_start) == 0,
                "selected": selected,
                "watch_symbols": watch_symbols,
                "manual_watch_symbols": manual_watch_symbols,
                "manual_entry_exit_only_global": manual_entry_exit_only_global,
                "manual_entry_exit_only_symbols": sorted(
                    symbol for symbol, enabled in manual_entry_exit_only_by_symbol.items() if enabled
                ),
                "exec_disabled_watch_symbols": sorted(
                    symbol for symbol, enabled in exec_enabled_by_symbol.items() if not enabled
                ),
                "runtime_config_version": ROTATION_RUNTIME_CONFIG_VERSION,
                "selected_alpha_map": {
                    symbol: watch_alpha_map[symbol]
                    for symbol in selected
                    if symbol in watch_alpha_map
                },
                "selected_strategy_map": {
                    symbol: watch_strategy_map[symbol]
                    for symbol in selected
                    if watch_strategy_map.get(symbol)
                },
                "deferred_runtime_updates": deferred_runtime_updates,
                "deferred_watch_only": deferred_watch_only_symbols,
                "inventory_protected_watch_symbols": sorted(inventory_protected_watch_symbols),
                "managed_stopped_symbols": sorted(managed_stopped_symbols),
                "unmanaged_stopped_symbols": sorted(unmanaged_stopped_symbols),
                "unmanaged_inventory_protected_symbols": sorted(unmanaged_inventory_protected_symbols),
                "selected_shared_equity_est_usdc": selected_shared_equity,
                "selected_expected_notional_map": selected_expected_notional_map,
                "runtime_budget_overrides": runtime_budget_overrides,
                "orphan_process_cleanup": orphan_cleanup,
                "failed_start": failed_start,
            },
            ensure_ascii=True,
        )
    )


if __name__ == "__main__":
    main()

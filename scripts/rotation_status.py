#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
ACTIVE_FILE = REPO_ROOT / "configs" / "rotation_active_lanes.json"
DEFAULT_SNAPSHOT = REPO_ROOT / "logs" / "rotation_status.txt"


def _fmt_pct(value: float) -> str:
    return f"{value * 100.0:.1f}%"


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _gate_p1_sweet(row: dict) -> bool:
    reason = str(row.get("gate_reason", "") or "")
    if reason == "slope_profile_mismatch":
        return False
    return bool(row.get("slope_profile_match", True))


def _gate_p2_makro(row: dict) -> bool:
    reason = str(row.get("gate_reason", "") or "")
    if reason and reason not in {"-", "keep_open", "spread", "depth", "volume", "slope_profile_mismatch"}:
        return False
    return (
        not bool(row.get("macro_down_context"))
        and not bool(row.get("rebound_in_downtrend"))
        and not bool(row.get("countertrend_rebound"))
        and str(row.get("structure_phase", "")) != "downtrend"
    )


def _gate_p3_covol(row: dict) -> bool:
    reason = str(row.get("gate_reason", "") or "")
    if reason in {"spread", "depth", "volume"}:
        return False
    spread_ok = _safe_float(row.get("spread_bps"), 9999.0) <= 22.0
    depth_ok = _safe_float(row.get("top_depth_notional"), 0.0) >= 80.0
    q5 = _safe_float(row.get("quote_volume_5m"), 0.0)
    q60 = _safe_float(row.get("quote_volume_60m"), 0.0)
    volume_ok = not (q5 < 8.0 and q60 < 1500.0)
    return spread_ok and depth_ok and volume_ok


def _display_gate_reason(row: dict) -> str:
    base_reason = str(row.get("gate_reason", "") or "-")
    if bool(row.get("keep_open")):
        return "keep_open"
    if bool(row.get("selected_active")):
        selection_path = str(row.get("selection_path", "") or "")
        selection_strategy = str(
            row.get("selected_strategy", "") or row.get("strategy_primary", "") or ""
        )
        if selection_path == "fast_track" and selection_strategy:
            return f"fast_track_{selection_strategy}"
        if selection_strategy and base_reason in {"", "-"}:
            return f"selected_{selection_strategy}"
    return base_reason


def _gate_status_row(row: dict) -> str:
    def mark(ok: bool) -> str:
        return "OK" if ok else "--"

    p1 = mark(_gate_p1_sweet(row))
    p2 = mark(_gate_p2_makro(row))
    p3 = mark(_gate_p3_covol(row))
    reason = _display_gate_reason(row)
    return f"         1 Sweet {p1}   2 Makro {p2}   3 Co/Vol {p3}   gate {reason}"


def _load_state() -> dict:
    if not ACTIVE_FILE.exists():
        return {}
    try:
        return json.loads(ACTIVE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _format_row(row: dict) -> str:
    symbol = str(row.get("symbol", "?")).ljust(8)
    lane = str(row.get("selected_strategy", "") or row.get("strategy_primary", "") or "-").ljust(12)
    score = f"{float(row.get('score', 0.0)):7.2f}"
    spread = f"{float(row.get('spread_bps', 0.0)):6.1f}"
    ret15 = f"{float(row.get('ret15_bps', 0.0)):7.1f}"
    rel15 = f"{float(row.get('rel15_bps', 0.0)):7.1f}"
    corr = f"{float(row.get('corr_btc', 1.0)):5.2f}"
    open_n = f"{float(row.get('open_notional', 0.0)):6.2f}"
    line1 = (
        f"{symbol} lane {lane} score {score}  spread {spread}  "
        f"r15 {ret15}  rel15 {rel15}  btc {corr}  open {open_n}"
    )
    return f"{line1}\n{_gate_status_row(row)}"


def render_status() -> str:
    state = _load_state()
    if not state:
        return "rotation: no active selection file found"

    generated_at = str(state.get("generated_at", "unknown"))
    selected = [str(x).upper() for x in state.get("selected", [])]
    fraction = float(state.get("fraction", 0.0) or 0.0)
    rows = state.get("rows") or []
    all_rows = state.get("all_rows") or []

    lines: list[str] = []
    lines.append(f"rotation  updated {generated_at}")
    lines.append(
        "active    "
        + (", ".join(selected) if selected else "(none)")
        + f"   share {_fmt_pct(fraction) if selected else '0.0%'}"
    )
    lines.append("table     symbol / lane / 1 Sweet / 2 Makro / 3 Co/Vol")
    lines.append("gates     1 Sweet   2 Makro   3 Co/Vol")
    lines.append("")
    lines.append("active set")
    if rows:
        for row in rows:
            lines.append(_format_row(row))
    else:
        lines.append("(none)")

    eligible_rest = [
        row
        for row in all_rows
        if bool(row.get("eligible")) and str(row.get("symbol", "")).upper() not in selected
    ]
    if eligible_rest:
        lines.append("")
        lines.append("next up")
        for row in eligible_rest[:4]:
            lines.append(_format_row(row))

    blocked = [row for row in all_rows if not bool(row.get("eligible"))]
    if blocked:
        lines.append("")
        lines.append("blocked")
        for row in blocked[:4]:
            symbol = str(row.get("symbol", "?")).ljust(8)
            reason = _display_gate_reason(row)
            spread = f"{float(row.get('spread_bps', 0.0)):6.1f}"
            lines.append(f"{symbol} reason {reason:<18} spread {spread}")
            lines.append(_gate_status_row(row))

    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="Render the current 4-coin rotation selection")
    ap.add_argument(
        "--write",
        default="",
        help=f"Optional snapshot path (default suggested: {DEFAULT_SNAPSHOT})",
    )
    args = ap.parse_args()

    text = render_status() + "\n"
    if args.write:
        out_path = Path(args.write)
        if not out_path.is_absolute():
            out_path = (REPO_ROOT / out_path).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")

    print(text, end="")


if __name__ == "__main__":
    main()

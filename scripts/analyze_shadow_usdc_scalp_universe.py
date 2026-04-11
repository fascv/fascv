#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from typing import Any

if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trading.meta.strategy_views import STRATEGY_NAMES, annotate_row_with_strategy_views


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_SNAPSHOT_DIR = REPO_ROOT / "logs" / "shadow_usdc_scalp_snapshots"
DEFAULT_REPORT_JSON = REPO_ROOT / "logs" / "shadow_usdc_scalp_report.json"
DEFAULT_REPORT_CSV = REPO_ROOT / "logs" / "shadow_usdc_scalp_report.csv"
HARD_BLOCK_REASONS = {
    "spread",
    "depth",
    "volume",
    "still_dumping",
    "post_dump_recovery_pending",
    "coin_peak_reentry_pending",
    "overextended",
    "macro_downtrend",
    "rebound_in_downtrend",
    "countertrend_rebound",
    "structure_peak",
    "structure_rollover",
    "structure_downtrend",
}


def _parse_iso(raw: object) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            return datetime.fromisoformat(text[:-1] + "+00:00")
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _mean(values: list[float]) -> float:
    return sum(values) / float(len(values)) if values else 0.0


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    seq = sorted(values)
    mid = len(seq) // 2
    if len(seq) % 2:
        return seq[mid]
    return (seq[mid - 1] + seq[mid]) / 2.0


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    seq = sorted(values)
    if len(seq) == 1:
        return seq[0]
    pos = max(0.0, min(1.0, float(q))) * (len(seq) - 1)
    low = int(math.floor(pos))
    high = int(math.ceil(pos))
    if low == high:
        return seq[low]
    frac = pos - low
    return seq[low] + ((seq[high] - seq[low]) * frac)


def _ratio(count: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return float(count) / float(total)


def _iter_snapshots(snapshot_dir: Path, cutoff: datetime | None) -> list[tuple[Path, datetime, dict[str, Any]]]:
    out: list[tuple[Path, datetime, dict[str, Any]]] = []
    for path in sorted(snapshot_dir.glob("shadow_usdc_scalp_*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        ts = _parse_iso(payload.get("generated_at")) or datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc
        )
        if cutoff is not None and ts < cutoff:
            continue
        out.append((path, ts, payload))
    out.sort(key=lambda item: item[1])
    return out


def _bucket_symbol(
    *,
    scans: int,
    min_scans: int,
    avg_score: float,
    eligible_ratio: float,
    trend_candidate_ratio: float,
    short_horizon_scalp_ok_ratio: float,
    macro_up_ratio: float,
    macro_down_ratio: float,
    hard_block_ratio: float,
    p95_spread_bps: float,
    median_depth_notional: float,
) -> str:
    if scans < min_scans:
        return "insufficient_data"
    if p95_spread_bps >= 30.0 or hard_block_ratio >= 0.80 or avg_score <= -9300.0:
        return "hard_reject"
    if (
        trend_candidate_ratio >= 0.30
        and eligible_ratio >= 0.20
        and macro_down_ratio <= 0.35
        and p95_spread_bps <= 18.0
        and median_depth_notional >= 70.0
    ):
        return "core_keep"
    if (
        trend_candidate_ratio >= 0.12
        and eligible_ratio >= 0.08
        and hard_block_ratio <= 0.55
        and p95_spread_bps <= 24.0
        and median_depth_notional >= 55.0
    ):
        return "keep"
    if short_horizon_scalp_ok_ratio >= 0.10 or macro_up_ratio >= 0.15 or avg_score >= -8200.0:
        return "review"
    return "reject"


def _shadow_rank(
    *,
    avg_score: float,
    eligible_ratio: float,
    trend_candidate_ratio: float,
    macro_up_ratio: float,
    macro_down_ratio: float,
    short_horizon_scalp_ok_ratio: float,
    strong_continuation_ratio: float,
    p95_spread_bps: float,
    median_depth_notional: float,
    hard_block_ratio: float,
    ) -> float:
    return (
        avg_score
        + (eligible_ratio * 1800.0)
        + (trend_candidate_ratio * 1400.0)
        + (macro_up_ratio * 320.0)
        + (short_horizon_scalp_ok_ratio * 260.0)
        + (strong_continuation_ratio * 180.0)
        - (macro_down_ratio * 620.0)
        - (hard_block_ratio * 820.0)
        - (max(0.0, p95_spread_bps - 8.0) * 24.0)
        - (max(0.0, 70.0 - median_depth_notional) * 2.2)
    )


def _strategy_rank_aggregate(row: dict[str, Any], strategy: str) -> float:
    latest_scores = row.get("latest_strategy_scores") if isinstance(row.get("latest_strategy_scores"), dict) else {}
    avg_scores = row.get("strategy_score_avgs") if isinstance(row.get("strategy_score_avgs"), dict) else {}
    ratios = row.get("strategy_candidate_ratios") if isinstance(row.get("strategy_candidate_ratios"), dict) else {}
    latest_score = _safe_float(latest_scores.get(strategy))
    avg_score = _safe_float(avg_scores.get(strategy))
    ratio = _safe_float(ratios.get(strategy))
    return (
        latest_score
        + (avg_score * 0.7)
        + (_safe_float(row.get("shadow_rank")) * 0.08)
        + (ratio * 60.0)
        + (_safe_float(row.get("macro_up_ratio")) * 30.0)
        - (_safe_float(row.get("macro_down_ratio")) * 45.0)
        - (_safe_float(row.get("hard_block_ratio")) * 55.0)
        - (max(0.0, _safe_float(row.get("p95_spread_bps")) - 8.0) * 3.0)
    )


def _build_strategy_rankings(summary_rows: list[dict[str, Any]], limit: int = 25) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for strategy in STRATEGY_NAMES:
        ranked = [
            row
            for row in summary_rows
            if _safe_float((row.get("strategy_candidate_ratios") or {}).get(strategy)) > 0.0
        ]
        ranked.sort(
            key=lambda row: _strategy_rank_aggregate(row, strategy),
            reverse=True,
        )
        out[strategy] = [
            {
                "symbol": str(row.get("symbol", "")).upper(),
                "rank_score": round(_strategy_rank_aggregate(row, strategy), 6),
                "avg_strategy_score": round(
                    _safe_float((row.get("strategy_score_avgs") or {}).get(strategy)),
                    6,
                ),
                "latest_strategy_score": round(
                    _safe_float((row.get("latest_strategy_scores") or {}).get(strategy)),
                    6,
                ),
                "strategy_candidate_ratio": round(
                    _safe_float((row.get("strategy_candidate_ratios") or {}).get(strategy)),
                    6,
                ),
                "shadow_rank": round(_safe_float(row.get("shadow_rank")), 6),
                "bucket": str(row.get("bucket", "")),
                "dominant_gate_reason": str(row.get("dominant_gate_reason", "")),
                "latest_gate_reason": str(row.get("latest_gate_reason", "")),
            }
            for row in ranked[: max(1, int(limit))]
        ]
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Aggregate shadow scalp selector snapshots and build a recommended USDC pool."
        )
    )
    parser.add_argument("--snapshot-dir", default=str(DEFAULT_SNAPSHOT_DIR))
    parser.add_argument("--lookback-hours", type=float, default=24.0)
    parser.add_argument("--top-keep", type=int, default=100)
    parser.add_argument("--min-scans", type=int, default=4)
    parser.add_argument("--out-json", default=str(DEFAULT_REPORT_JSON))
    parser.add_argument("--out-csv", default=str(DEFAULT_REPORT_CSV))
    args = parser.parse_args()

    snapshot_dir = Path(str(args.snapshot_dir)).resolve()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(0.0, float(args.lookback_hours)))
    snapshots = _iter_snapshots(snapshot_dir, cutoff)
    if not snapshots:
        raise SystemExit(f"no snapshots found in {snapshot_dir} for last {args.lookback_hours}h")

    per_symbol: dict[str, dict[str, Any]] = {}
    quote_asset = "USDC"
    for _, _, payload in snapshots:
        quote_asset = str(payload.get("quote_asset") or quote_asset).upper()
        rows = payload.get("rows")
        if not isinstance(rows, list):
            continue
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            annotate_row_with_strategy_views(raw)
            symbol = str(raw.get("symbol", "")).strip().upper()
            if not symbol:
                continue
            acc = per_symbol.setdefault(
                symbol,
                {
                    "symbol": symbol,
                    "market": str(raw.get("market", "")),
                    "scores": [],
                    "spreads": [],
                    "depths": [],
                    "quote_volume_60m": [],
                    "quote_volume_5m": [],
                    "gate_reason_counts": Counter(),
                    "setup_type_counts": Counter(),
                    "scans": 0,
                    "eligible": 0,
                    "trend_candidate": 0,
                    "macro_up": 0,
                    "macro_down": 0,
                    "short_horizon_scalp_ok": 0,
                    "strong_continuation": 0,
                    "broad_uptrend": 0,
                    "strategy_hits": {name: 0 for name in STRATEGY_NAMES},
                    "strategy_score_sums": {name: 0.0 for name in STRATEGY_NAMES},
                    "strategy_primary_counts": Counter(),
                    "latest_row": None,
                },
            )
            acc["scans"] += 1
            acc["scores"].append(_safe_float(raw.get("score")))
            acc["spreads"].append(_safe_float(raw.get("spread_bps")))
            acc["depths"].append(_safe_float(raw.get("top_depth_notional")))
            acc["quote_volume_60m"].append(_safe_float(raw.get("quote_volume_60m")))
            acc["quote_volume_5m"].append(_safe_float(raw.get("quote_volume_5m")))
            gate_reason = str(raw.get("gate_reason", "")).strip()
            if gate_reason:
                acc["gate_reason_counts"][gate_reason] += 1
            setup_type = str(raw.get("setup_type", "")).strip()
            if setup_type:
                acc["setup_type_counts"][setup_type] += 1
            if bool(raw.get("eligible")):
                acc["eligible"] += 1
            if bool(raw.get("trend_candidate")):
                acc["trend_candidate"] += 1
            if bool(raw.get("macro_up_context")):
                acc["macro_up"] += 1
            if bool(raw.get("macro_down_context")):
                acc["macro_down"] += 1
            if bool(raw.get("short_horizon_scalp_ok")):
                acc["short_horizon_scalp_ok"] += 1
            if bool(raw.get("strong_continuation_context")):
                acc["strong_continuation"] += 1
            if bool(raw.get("broad_uptrend_context")):
                acc["broad_uptrend"] += 1
            strategy_scores = raw.get("strategy_scores") if isinstance(raw.get("strategy_scores"), dict) else {}
            for strategy in STRATEGY_NAMES:
                strategy_score = _safe_float(strategy_scores.get(strategy))
                if strategy_score > 0.0:
                    acc["strategy_hits"][strategy] += 1
                    acc["strategy_score_sums"][strategy] += strategy_score
            primary_strategy = str(raw.get("strategy_primary", "")).strip()
            if primary_strategy:
                acc["strategy_primary_counts"][primary_strategy] += 1
            acc["latest_row"] = raw

    summary_rows: list[dict[str, Any]] = []
    for symbol, acc in per_symbol.items():
        scans = int(acc["scans"])
        avg_score = _mean(acc["scores"])
        median_score = _median(acc["scores"])
        p75_score = _quantile(acc["scores"], 0.75)
        avg_spread_bps = _mean(acc["spreads"])
        p95_spread_bps = _quantile(acc["spreads"], 0.95)
        median_depth_notional = _median(acc["depths"])
        median_quote_volume_60m = _median(acc["quote_volume_60m"])
        median_quote_volume_5m = _median(acc["quote_volume_5m"])
        eligible_ratio = _ratio(int(acc["eligible"]), scans)
        trend_candidate_ratio = _ratio(int(acc["trend_candidate"]), scans)
        macro_up_ratio = _ratio(int(acc["macro_up"]), scans)
        macro_down_ratio = _ratio(int(acc["macro_down"]), scans)
        short_horizon_scalp_ok_ratio = _ratio(int(acc["short_horizon_scalp_ok"]), scans)
        strong_continuation_ratio = _ratio(int(acc["strong_continuation"]), scans)
        broad_uptrend_ratio = _ratio(int(acc["broad_uptrend"]), scans)
        hard_block_count = sum(
            int(count)
            for reason, count in acc["gate_reason_counts"].items()
            if str(reason) in HARD_BLOCK_REASONS
        )
        hard_block_ratio = _ratio(hard_block_count, scans)
        dominant_gate_reason = ""
        if acc["gate_reason_counts"]:
            dominant_gate_reason = str(acc["gate_reason_counts"].most_common(1)[0][0])
        dominant_setup_type = ""
        if acc["setup_type_counts"]:
            dominant_setup_type = str(acc["setup_type_counts"].most_common(1)[0][0])
        strategy_candidate_ratios = {
            strategy: _ratio(int(acc["strategy_hits"].get(strategy, 0)), scans)
            for strategy in STRATEGY_NAMES
        }
        strategy_score_avgs = {
            strategy: (
                _safe_float(acc["strategy_score_sums"].get(strategy))
                / float(max(1, int(acc["strategy_hits"].get(strategy, 0))))
                if int(acc["strategy_hits"].get(strategy, 0)) > 0
                else 0.0
            )
            for strategy in STRATEGY_NAMES
        }
        strategy_primary = ""
        strategy_primary_share = 0.0
        if acc["strategy_primary_counts"]:
            strategy_primary, strategy_primary_count = acc["strategy_primary_counts"].most_common(1)[0]
            strategy_primary_share = _ratio(int(strategy_primary_count), scans)
        latest_row = acc["latest_row"] or {}
        latest_strategy_scores = (
            dict(latest_row.get("strategy_scores"))
            if isinstance(latest_row.get("strategy_scores"), dict)
            else {}
        )
        bucket = _bucket_symbol(
            scans=scans,
            min_scans=max(1, int(args.min_scans)),
            avg_score=avg_score,
            eligible_ratio=eligible_ratio,
            trend_candidate_ratio=trend_candidate_ratio,
            short_horizon_scalp_ok_ratio=short_horizon_scalp_ok_ratio,
            macro_up_ratio=macro_up_ratio,
            macro_down_ratio=macro_down_ratio,
            hard_block_ratio=hard_block_ratio,
            p95_spread_bps=p95_spread_bps,
            median_depth_notional=median_depth_notional,
        )
        shadow_rank = _shadow_rank(
            avg_score=avg_score,
            eligible_ratio=eligible_ratio,
            trend_candidate_ratio=trend_candidate_ratio,
            macro_up_ratio=macro_up_ratio,
            macro_down_ratio=macro_down_ratio,
            short_horizon_scalp_ok_ratio=short_horizon_scalp_ok_ratio,
            strong_continuation_ratio=strong_continuation_ratio,
            p95_spread_bps=p95_spread_bps,
            median_depth_notional=median_depth_notional,
            hard_block_ratio=hard_block_ratio,
        )
        summary_rows.append(
            {
                "symbol": symbol,
                "market": acc["market"] or f"{symbol}{quote_asset}",
                "bucket": bucket,
                "shadow_rank": shadow_rank,
                "avg_score": avg_score,
                "median_score": median_score,
                "p75_score": p75_score,
                "eligible_ratio": eligible_ratio,
                "trend_candidate_ratio": trend_candidate_ratio,
                "macro_up_ratio": macro_up_ratio,
                "macro_down_ratio": macro_down_ratio,
                "short_horizon_scalp_ok_ratio": short_horizon_scalp_ok_ratio,
                "strong_continuation_ratio": strong_continuation_ratio,
                "broad_uptrend_ratio": broad_uptrend_ratio,
                "hard_block_ratio": hard_block_ratio,
                "avg_spread_bps": avg_spread_bps,
                "p95_spread_bps": p95_spread_bps,
                "median_depth_notional": median_depth_notional,
                "median_quote_volume_60m": median_quote_volume_60m,
                "median_quote_volume_5m": median_quote_volume_5m,
                "dominant_gate_reason": dominant_gate_reason,
                "dominant_setup_type": dominant_setup_type,
                "strategy_primary": strategy_primary,
                "strategy_primary_share": strategy_primary_share,
                "strategy_candidate_ratios": strategy_candidate_ratios,
                "strategy_score_avgs": strategy_score_avgs,
                "latest_strategy_primary": str(latest_row.get("strategy_primary", "") or ""),
                "latest_strategy_scores": latest_strategy_scores,
                "scans": scans,
                "latest_gate_reason": str(latest_row.get("gate_reason", "")),
                "latest_structure_phase": str(latest_row.get("structure_phase", "")),
                "latest_setup_type": str(latest_row.get("setup_type", "")),
            }
        )

    summary_rows.sort(key=lambda item: float(item["shadow_rank"]), reverse=True)
    bucket_counts = Counter(str(row["bucket"]) for row in summary_rows)
    keep_rows = [row for row in summary_rows if str(row["bucket"]) in {"core_keep", "keep"}]
    review_rows = [row for row in summary_rows if str(row["bucket"]) == "review"]
    recommended_rows = keep_rows[: max(1, int(args.top_keep))]
    if len(recommended_rows) < int(args.top_keep):
        shortfall = int(args.top_keep) - len(recommended_rows)
        recommended_rows.extend(review_rows[:shortfall])
    strategy_rankings = _build_strategy_rankings(summary_rows)
    strategy_summary = {
        strategy: {
            "candidate_count": sum(
                1
                for row in summary_rows
                if _safe_float((row.get("strategy_candidate_ratios") or {}).get(strategy)) > 0.0
            ),
            "top_symbols": [item["symbol"] for item in strategy_rankings.get(strategy, [])[:10]],
        }
        for strategy in STRATEGY_NAMES
    }

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "lookback_hours": float(args.lookback_hours),
        "snapshot_dir": str(snapshot_dir),
        "snapshots_used": len(snapshots),
        "first_snapshot_at": snapshots[0][1].isoformat().replace("+00:00", "Z"),
        "last_snapshot_at": snapshots[-1][1].isoformat().replace("+00:00", "Z"),
        "quote_asset": quote_asset,
        "symbol_count": len(summary_rows),
        "bucket_counts": dict(bucket_counts),
        "recommended_pool_size": len(recommended_rows),
        "recommended_pool": [row["symbol"] for row in recommended_rows],
        "strategy_summary": strategy_summary,
        "strategy_rankings": strategy_rankings,
        "top_candidates": recommended_rows[:25],
        "hard_rejects": [row for row in summary_rows if str(row["bucket"]) == "hard_reject"][:25],
        "rows": summary_rows,
    }

    out_json = Path(str(args.out_json)).resolve()
    out_csv = Path(str(args.out_csv)).resolve()
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")

    fieldnames = [
        "symbol",
        "market",
        "bucket",
        "shadow_rank",
        "avg_score",
        "median_score",
        "p75_score",
        "eligible_ratio",
        "trend_candidate_ratio",
        "macro_up_ratio",
        "macro_down_ratio",
        "short_horizon_scalp_ok_ratio",
        "strong_continuation_ratio",
        "broad_uptrend_ratio",
        "hard_block_ratio",
        "avg_spread_bps",
        "p95_spread_bps",
        "median_depth_notional",
        "median_quote_volume_60m",
        "median_quote_volume_5m",
        "dominant_gate_reason",
        "dominant_setup_type",
        "strategy_primary",
        "strategy_primary_share",
        "latest_strategy_primary",
        "scans",
        "latest_gate_reason",
        "latest_structure_phase",
        "latest_setup_type",
    ]
    with out_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in summary_rows:
            writer.writerow({key: row.get(key) for key in fieldnames})

    print(
        json.dumps(
            {
                "ok": True,
                "snapshots_used": len(snapshots),
                "symbol_count": len(summary_rows),
                "recommended_pool_size": len(recommended_rows),
                "out_json": str(out_json),
                "out_csv": str(out_csv),
            },
            ensure_ascii=True,
        )
    )


if __name__ == "__main__":
    main()

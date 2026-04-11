from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from btc_news_arrow.aggregator import Aggregator
from btc_news_arrow.alerts import format_alert_summary, run_alert_checks, send_webhook, should_fail
from btc_news_arrow.benchmark import format_benchmark_summary, run_benchmark
from btc_news_arrow.classifier import Classifier
from btc_news_arrow.collector import Collector
from btc_news_arrow.config import load_config
from btc_news_arrow.hybrid_report import (
    format_hybrid_report_summary,
    generate_hybrid_eval_report,
)
from btc_news_arrow.learner import Learner
from btc_news_arrow.llm_rater import LLMRater
from btc_news_arrow.optimizer import evaluate_hybrid_model, optimize_hybrid_weights, optimize_llm_config
from btc_news_arrow.regression import (
    calibrate_regression_baseline,
    format_calibration_summary,
    format_regression_summary,
    run_regression_check,
)
from btc_news_arrow.source_quality import (
    format_source_quality_summary,
    generate_source_quality_report,
)
from btc_news_arrow.scorer import Scorer
from btc_news_arrow.storage import Storage
from btc_news_arrow.utils import parse_datetime, parse_duration, utcnow


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="btcnews", description="BTC news arrow engine")
    parser.add_argument("--config", default="config.yaml", help="Path to config YAML")
    parser.add_argument("--db", default="btc_news_arrow.db", help="Path to SQLite DB")

    sub = parser.add_subparsers(dest="command", required=True)

    collect = sub.add_parser("collect", help="Collect feeds and persist scored items")
    collect.add_argument("--no-gdelt", action="store_true", help="Disable GDELT for this run")

    sub.add_parser("rescore", help="Recompute category/polarity/impact for all stored items")

    learn_update = sub.add_parser("learn-update", help="Update online learning labels/model from stored items")
    learn_update.add_argument("--limit", type=int, default=500, help="Max unlabeled items per horizon to process")

    arrow = sub.add_parser("arrow", help="Print BTC impact arrow for one or many windows")
    arrow.add_argument("--window", help="Duration like 1h/24h; if omitted prints 1h,24h")

    learn_arrow = sub.add_parser("learn-arrow", help="Print learning-based BTC impact arrows")
    learn_arrow.add_argument("--window", help="Duration like 1h/24h; if omitted prints 1h,24h")

    llm_arrow = sub.add_parser("llm-arrow", help="Print LLM-based BTC impact arrows")
    llm_arrow.add_argument("--window", help="Duration like 1h/24h; if omitted prints 1h,24h")
    sub.add_parser("llm-ping", help="Run a direct OpenAI ping request")
    llm_opt = sub.add_parser("llm-optimize", help="Auto-tune LLM parameters using live attempts")
    llm_opt.add_argument("--cycles", type=int, default=3, help="How many optimization cycles to run")
    llm_opt.add_argument("--sleep", type=int, default=20, help="Seconds between cycles")
    llm_opt.add_argument(
        "--windows",
        default="1h,24h",
        help="Comma separated windows (e.g. 1h,24h)",
    )
    hyb_opt = sub.add_parser("hybrid-optimize", help="Fit hybrid rule/learn weights to realized BTC returns")
    hyb_opt.add_argument(
        "--windows",
        default="1h,24h",
        help="Comma separated windows (e.g. 1h,24h)",
    )
    hyb_opt.add_argument("--lookback-days", type=int, default=30, help="History days for fitting")
    hyb_opt.add_argument("--min-samples", type=int, default=120, help="Minimum labeled samples")
    hyb_opt.add_argument("--grid-step", type=float, default=0.05, help="Weight search step")
    hyb_eval = sub.add_parser("hybrid-eval", help="Evaluate rule/learn/hybrid quality on labeled history")
    hyb_eval.add_argument(
        "--windows",
        default="1h,24h",
        help="Comma separated windows (e.g. 1h,24h)",
    )
    hyb_eval.add_argument("--lookback-days", type=int, default=30, help="History days for evaluation")
    hyb_eval.add_argument("--min-samples", type=int, default=120, help="Minimum labeled samples")
    hyb_report = sub.add_parser("hybrid-report", help="Run hybrid-eval and persist trend report")
    hyb_report.add_argument(
        "--windows",
        default="1h,24h",
        help="Comma separated windows (e.g. 1h,24h)",
    )
    hyb_report.add_argument("--lookback-days", type=int, default=30, help="History days for evaluation")
    hyb_report.add_argument("--min-samples", type=int, default=120, help="Minimum labeled samples")
    hyb_report.add_argument(
        "--report-path",
        default="diagnostics/hybrid_eval_latest.json",
        help="Path to write latest hybrid report JSON",
    )
    hyb_report.add_argument(
        "--history-dir",
        default="diagnostics/hybrid_eval_history",
        help="Directory to write timestamped report snapshots",
    )
    hyb_report.add_argument("--no-history", action="store_true", help="Disable writing timestamped history snapshots")
    hyb_report.add_argument("--json", action="store_true", help="Print full JSON report")
    src_quality = sub.add_parser("source-quality-report", help="Evaluate realized signal quality by source")
    src_quality.add_argument(
        "--windows",
        default="1h,24h",
        help="Comma separated windows (e.g. 1h,24h)",
    )
    src_quality.add_argument("--lookback-days", type=int, default=30, help="History days for evaluation")
    src_quality.add_argument("--min-samples-per-source", type=int, default=8, help="Minimum samples per source")
    src_quality.add_argument("--top-n", type=int, default=25, help="Max sources per window in report")
    src_quality.add_argument(
        "--report-path",
        default="diagnostics/source_quality_latest.json",
        help="Path to write latest source-quality report JSON",
    )
    src_quality.add_argument(
        "--history-dir",
        default="diagnostics/source_quality_history",
        help="Directory to write timestamped source-quality snapshots",
    )
    src_quality.add_argument("--no-history", action="store_true", help="Disable writing timestamped history snapshots")
    src_quality.add_argument("--json", action="store_true", help="Print full JSON report")
    bench = sub.add_parser("benchmark", help="Run local throughput benchmarks (dedupe + label upserts)")
    bench.add_argument(
        "--bench-db",
        default="btc_news_arrow_bench.db",
        help="SQLite file used only for benchmark runs",
    )
    bench.add_argument("--dedupe-existing", type=int, default=5000, help="Synthetic existing items for dedupe benchmark")
    bench.add_argument("--dedupe-probes", type=int, default=2000, help="How many dedupe checks to run")
    bench.add_argument("--label-rows", type=int, default=20000, help="Rows for label upsert benchmark")
    bench.add_argument("--label-batch-size", type=int, default=1000, help="Batch size for label upsert benchmark")
    bench.add_argument("--keep-db", action="store_true", help="Keep benchmark DB file")
    bench.add_argument("--overwrite-db", action="store_true", help="Allow replacing existing benchmark DB file")
    bench.add_argument("--force-db", action="store_true", help="Allow using a production-like DB filename")
    bench.add_argument("--json", action="store_true", help="Print full JSON summary")
    reg = sub.add_parser("regression-check", help="Run CI-style regression gates against baseline thresholds")
    reg.add_argument("--baseline", default="tools/regression_baseline.json", help="Path to regression baseline JSON")
    reg.add_argument("--perf-floor-ratio", type=float, default=0.7, help="Required fraction of baseline for perf checks")
    reg.add_argument("--bench-db", default="btc_news_arrow_regression_bench.db", help="Temporary DB path for perf checks")
    reg.add_argument(
        "--dedupe-existing",
        type=int,
        default=None,
        help="Synthetic existing items for dedupe benchmark (default: baseline profile)",
    )
    reg.add_argument(
        "--dedupe-probes",
        type=int,
        default=None,
        help="How many dedupe checks to run (default: baseline profile)",
    )
    reg.add_argument(
        "--label-rows",
        type=int,
        default=None,
        help="Rows for label upsert benchmark (default: baseline profile)",
    )
    reg.add_argument(
        "--label-batch-size",
        type=int,
        default=None,
        help="Batch size for label upsert benchmark (default: baseline profile)",
    )
    reg.add_argument("--check-hybrid-eval", action="store_true", help="Also gate hybrid-eval metrics")
    reg.add_argument("--require-hybrid-samples", action="store_true", help="Fail if hybrid eval has too few samples")
    reg.add_argument("--hybrid-windows", default="1h,24h", help="Windows for optional hybrid-eval gate")
    reg.add_argument("--hybrid-lookback-days", type=int, default=30, help="Lookback days for optional hybrid-eval gate")
    reg.add_argument("--hybrid-min-samples", type=int, default=120, help="Min samples for optional hybrid-eval gate")
    reg.add_argument("--json", action="store_true", help="Print full JSON summary")
    cal = sub.add_parser("baseline-calibrate", help="Calibrate and write regression baseline from repeated benchmark runs")
    cal.add_argument("--output", default="tools/regression_baseline.json", help="Path to write baseline JSON")
    cal.add_argument("--runs", type=int, default=5, help="How many benchmark runs to aggregate")
    cal.add_argument("--dedupe-existing", type=int, default=1500, help="Synthetic existing items for dedupe benchmark")
    cal.add_argument("--dedupe-probes", type=int, default=800, help="How many dedupe checks to run")
    cal.add_argument("--label-rows", type=int, default=6000, help="Rows for label upsert benchmark")
    cal.add_argument("--label-batch-size", type=int, default=500, help="Batch size for label upsert benchmark")
    cal.add_argument("--include-hybrid-eval", action="store_true", help="Also derive hybrid-eval floors from current labeled data")
    cal.add_argument("--hybrid-windows", default="1h,24h", help="Windows for optional hybrid-eval calibration")
    cal.add_argument("--hybrid-lookback-days", type=int, default=30, help="Lookback days for optional hybrid-eval calibration")
    cal.add_argument("--hybrid-min-samples", type=int, default=120, help="Min samples for optional hybrid-eval calibration")
    cal.add_argument("--hybrid-corr-margin", type=float, default=0.05, help="Margin subtracted from measured hybrid corr")
    cal.add_argument("--hybrid-directional-margin", type=float, default=0.05, help="Margin subtracted from measured hybrid directional accuracy")
    cal.add_argument("--overwrite", action="store_true", help="Allow overwriting output baseline file")
    cal.add_argument("--json", action="store_true", help="Print full JSON summary")
    alerts = sub.add_parser("alerts-check", help="Run operational health/quality/drift alert checks")
    alerts.add_argument("--hybrid-report-path", default="diagnostics/hybrid_eval_latest.json", help="Path to latest hybrid report JSON")
    alerts.add_argument("--hybrid-history-dir", default="diagnostics/hybrid_eval_history", help="Directory with hybrid report snapshots")
    alerts.add_argument("--source-quality-report-path", default="diagnostics/source_quality_latest.json", help="Path to latest source-quality report JSON")
    alerts.add_argument("--source-quality-history-dir", default="diagnostics/source_quality_history", help="Directory with source-quality report snapshots")
    alerts.add_argument("--freshness-minutes", type=int, default=30, help="Critical if no fresh items in this window")
    alerts.add_argument("--window-minutes", type=int, default=60, help="Window for volume and drift checks")
    alerts.add_argument("--min-items", type=int, default=3, help="High severity if fewer items than this threshold")
    alerts.add_argument(
        "--source-concentration-threshold",
        type=float,
        default=0.85,
        help="Drift alert when one source exceeds this share in window",
    )
    alerts.add_argument(
        "--hybrid-degraded-streak",
        type=int,
        default=2,
        help="Quality alert when hybrid trend is degraded for consecutive runs",
    )
    alerts.add_argument(
        "--source-quality-degraded-streak",
        type=int,
        default=2,
        help="Quality alert when source-quality trend is degraded for consecutive runs",
    )
    alerts.add_argument(
        "--source-quality-corr-drop-threshold",
        type=float,
        default=0.05,
        help="Quality alert when source-quality global corr delta drops below negative threshold",
    )
    alerts.add_argument("--output-path", default="diagnostics/alerts_latest.json", help="Path to write latest alerts JSON")
    alerts.add_argument("--history-dir", default="diagnostics/alerts_history", help="Directory to write timestamped alert snapshots")
    alerts.add_argument("--no-history", action="store_true", help="Disable writing timestamped alert snapshots")
    alerts.add_argument("--fail-on", default="critical,high,quality", help="Comma-separated severities that should return exit code 1")
    alerts.add_argument("--webhook-url", default="", help="Optional webhook URL for alert notifications")
    alerts.add_argument("--webhook-on", default="critical,high,quality", help="Comma-separated severities that should trigger webhook sends")
    alerts.add_argument("--webhook-timeout", type=int, default=5, help="Webhook request timeout in seconds")
    alerts.add_argument("--webhook-retries", type=int, default=2, help="Webhook retries after initial attempt")
    alerts.add_argument("--webhook-backoff-seconds", type=float, default=1.5, help="Webhook exponential backoff base in seconds")
    alerts.add_argument("--webhook-cooldown-seconds", type=int, default=300, help="Cooldown to suppress duplicate webhook payloads")
    alerts.add_argument("--webhook-state-path", default="diagnostics/webhook_state.json", help="Path to webhook dedupe/cooldown state JSON")
    alerts.add_argument("--webhook-max-alerts", type=int, default=12, help="Maximum number of matching alerts sent per webhook payload")
    alerts.add_argument("--json", action="store_true", help="Print full JSON summary")

    forensic = sub.add_parser("forensic", help="List deduped items around a timestamp")
    forensic.add_argument("--ts", required=True, help="UTC timestamp (ISO-8601)")
    forensic.add_argument("--pm", default="30m", help="Plus/minus window (e.g. 30m)")

    serve = sub.add_parser("serve", help="Run optional FastAPI server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    return parser


def _duration_to_minutes(value: str) -> int:
    delta = parse_duration(value)
    mins = int(delta.total_seconds() // 60)
    if mins <= 0:
        raise ValueError("Window must be at least 1 minute")
    return mins


def _minutes_label(minutes: int) -> str:
    if minutes % (7 * 24 * 60) == 0:
        return f"{minutes // (7 * 24 * 60)}w"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def cmd_collect(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    storage = Storage(args.db)
    try:
        classifier = Classifier(config)
        scorer = Scorer(config)
        collector = Collector(config, storage, classifier, scorer)
        items, inserted, stats = collector.collect_once(include_gdelt=not args.no_gdelt)
        print(
            "Collected: "
            f"{len(items)} processed, {inserted} inserted, "
            f"raw={stats['totals']['raw_items']} errors={stats['totals']['errors']}"
        )
        return 0
    finally:
        storage.close()


def cmd_rescore(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    storage = Storage(args.db)
    try:
        classifier = Classifier(config)
        scorer = Scorer(config)
        now = utcnow()

        updates: list[tuple[int, str, int, float]] = []
        for item_id, item in storage.get_items_with_ids():
            item = classifier.classify(item)
            item = scorer.score(item, now=now)
            updates.append((item_id, item.category, item.polarity, item.impact))

        storage.update_item_analysis(updates)
        print(f"Rescored: {len(updates)} items")
        return 0
    finally:
        storage.close()


def cmd_arrow(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    storage = Storage(args.db)
    try:
        aggregator = Aggregator(config)
        try:
            if args.window:
                windows = [_duration_to_minutes(args.window)]
            else:
                windows = [60, 24 * 60]
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2

        now = utcnow()
        for w in windows:
            result = aggregator.arrow_from_storage(storage, window_minutes=w, now=now)
            print(f"BTC News Impact ({_minutes_label(w)}): {result.arrow}")
        return 0
    finally:
        storage.close()


def cmd_learn_update(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    storage = Storage(args.db)
    try:
        learner = Learner(config)
        summary = learner.update_model(storage=storage, limit_per_horizon=max(1, int(args.limit)))
        print(
            f"Learner updated: labeled={summary['labeled']} "
            f"clipped_labels={summary.get('clipped_labels', 0)} "
            f"feature_updates={summary['feature_updates']}"
        )
        return 0
    finally:
        storage.close()


def cmd_learn_arrow(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    storage = Storage(args.db)
    try:
        learner = Learner(config)
        try:
            if args.window:
                windows = [_duration_to_minutes(args.window)]
            else:
                windows = [60, 24 * 60]
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2

        now = utcnow()
        for w in windows:
            result = learner.learned_arrow(storage=storage, window_minutes=w, now=now)
            print(f"BTC Learn Impact ({_minutes_label(w)}): {result.arrow} (score {result.score:+.3f})")
        return 0
    finally:
        storage.close()


def cmd_forensic(args: argparse.Namespace) -> int:
    ts = parse_datetime(args.ts)
    if ts is None:
        print("Invalid --ts, expected ISO timestamp", file=sys.stderr)
        return 2

    try:
        pm = parse_duration(args.pm)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    storage = Storage(args.db)
    try:
        start = ts - pm
        end = ts + pm
        items = storage.get_items_between(start, end)
        print(f"Forensic window: {start.isoformat()} .. {end.isoformat()}")
        print(f"Items: {len(items)}")
        for item in items:
            impact = f"{item.impact:+.3f}"
            print(
                f"{item.timestamp_utc.isoformat()} | {item.source} | {item.category} | "
                f"{impact} | {item.title} | {item.url or '-'}"
            )
        return 0
    finally:
        storage.close()


def cmd_llm_arrow(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    storage = Storage(args.db)
    try:
        rater = LLMRater(config)
        try:
            if args.window:
                windows = [_duration_to_minutes(args.window)]
            else:
                windows = [60, 24 * 60]
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2

        now = utcnow()
        for w in windows:
            since = now - parse_duration(_minutes_label(w))
            items = storage.get_items_since(since)
            try:
                payload = rater.rate_window(items=items, window_minutes=w, now=now)
            except (RuntimeError, ValueError) as exc:
                print(f"LLM error ({_minutes_label(w)}): {exc}", file=sys.stderr)
                return 2
            print(
                f"BTC LLM Impact ({_minutes_label(w)}): {payload.result.arrow} "
                f"(score {payload.result.score:+.3f}, conf {payload.confidence:.2f})"
            )
        return 0
    finally:
        storage.close()


def cmd_llm_ping(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    rater = LLMRater(config)
    try:
        payload = rater.ping()
    except (RuntimeError, ValueError) as exc:
        print(f"LLM ping failed: {exc}", file=sys.stderr)
        return 2
    print(
        f"LLM ping OK | model={payload.get('model')} | "
        f"response_id={payload.get('response_id')} | preview={payload.get('preview')}"
    )
    return 0


def cmd_llm_optimize(args: argparse.Namespace) -> int:
    windows = [w.strip() for w in str(args.windows).split(",") if w.strip()]
    if not windows:
        windows = ["1h", "24h"]
    try:
        summary = optimize_llm_config(
            config_path=args.config,
            db_path=args.db,
            windows=windows,
            cycles=max(1, int(args.cycles)),
            sleep_seconds=max(0, int(args.sleep)),
        )
    except Exception as exc:
        print(f"LLM optimize failed: {exc}", file=sys.stderr)
        return 2

    print(f"LLM optimize done: ok={summary.get('ok')} updated={summary.get('updated')}")
    for cycle in summary.get("history", []):
        print(f"Cycle {cycle.get('cycle')}:")
        for att in cycle.get("attempts", []):
            meta = att.get("meta") or {}
            print(
                f"  {att.get('window')}: success={att.get('success')} "
                f"message={att.get('message')} model={meta.get('used_model')} "
                f"items={meta.get('used_items')} degraded={meta.get('degraded_profile')}"
            )
    return 0


def cmd_hybrid_optimize(args: argparse.Namespace) -> int:
    windows = [w.strip() for w in str(args.windows).split(",") if w.strip()]
    if not windows:
        windows = ["1h", "24h"]
    try:
        summary = optimize_hybrid_weights(
            config_path=args.config,
            db_path=args.db,
            windows=windows,
            lookback_days=max(1, int(args.lookback_days)),
            min_samples=max(20, int(args.min_samples)),
            grid_step=max(0.01, float(args.grid_step)),
        )
    except Exception as exc:
        print(f"Hybrid optimize failed: {exc}", file=sys.stderr)
        return 2

    if not summary.get("ok"):
        print(
            f"Hybrid optimize skipped: reason={summary.get('reason')} "
            f"samples={summary.get('samples')} min={summary.get('min_samples')}"
        )
        return 1

    weights = summary.get("weights") or {}
    print(
        "Hybrid optimize done: "
        f"samples={summary.get('samples')} "
        f"corr_before={summary.get('corr_before'):.4f} "
        f"corr_after={summary.get('corr_after'):.4f} "
        f"weights(rule={weights.get('rule_weight')}, llm={weights.get('llm_weight')}, learn={weights.get('learn_weight')})"
    )
    by_window = summary.get("by_window") or {}
    if by_window:
        rendered = ", ".join(f"{k}:{v}" for k, v in sorted(by_window.items()))
        print(f"  sample_split: {rendered}")
    return 0


def cmd_hybrid_eval(args: argparse.Namespace) -> int:
    windows = [w.strip() for w in str(args.windows).split(",") if w.strip()]
    if not windows:
        windows = ["1h", "24h"]
    try:
        summary = evaluate_hybrid_model(
            config_path=args.config,
            db_path=args.db,
            windows=windows,
            lookback_days=max(1, int(args.lookback_days)),
            min_samples=max(20, int(args.min_samples)),
        )
    except Exception as exc:
        print(f"Hybrid eval failed: {exc}", file=sys.stderr)
        return 2

    if not summary.get("ok"):
        print(
            f"Hybrid eval skipped: reason={summary.get('reason')} "
            f"samples={summary.get('samples')} min={summary.get('min_samples')}"
        )
        return 1

    weights = summary.get("weights_non_llm") or {}
    print(
        "Hybrid eval: "
        f"samples={summary.get('samples')} "
        f"weights_non_llm(rule={weights.get('rule_share')}, learn={weights.get('learn_share')})"
    )
    global_metrics = (summary.get("metrics") or {}).get("global") or {}
    for signal in ("rule", "learn", "hybrid"):
        m = global_metrics.get(signal) or {}
        print(
            f"  {signal}: corr={m.get('corr'):.4f} "
            f"dir_acc={m.get('directional_accuracy')} "
            f"mae={m.get('mae'):.6f}"
        )
    by_window = (summary.get("metrics") or {}).get("by_window") or {}
    for window in sorted(by_window.keys()):
        m_hybrid = (by_window.get(window) or {}).get("hybrid") or {}
        print(
            f"  {window}: hybrid_corr={m_hybrid.get('corr'):.4f} "
            f"hybrid_dir_acc={m_hybrid.get('directional_accuracy')} "
            f"hybrid_mae={m_hybrid.get('mae'):.6f}"
        )
    walk_forward = ((summary.get("metrics") or {}).get("walk_forward") or {}).get("global") or {}
    if walk_forward:
        adaptive = walk_forward.get("adaptive_hybrid") or {}
        current = walk_forward.get("current_hybrid") or {}
        trading = (walk_forward.get("trading") or {}).get("adaptive_hybrid") or {}
        print(
            "  walk_forward:"
            f" folds={walk_forward.get('folds')} oos_samples={walk_forward.get('samples_oos')} "
            f"curr_corr={current.get('corr')} adapt_corr={adaptive.get('corr')} "
            f"adapt_sharpe_like={trading.get('sharpe_like')}"
        )
    return 0


def cmd_hybrid_report(args: argparse.Namespace) -> int:
    windows = [w.strip() for w in str(args.windows).split(",") if w.strip()]
    if not windows:
        windows = ["1h", "24h"]
    try:
        report = generate_hybrid_eval_report(
            config_path=args.config,
            db_path=args.db,
            windows=windows,
            lookback_days=max(1, int(args.lookback_days)),
            min_samples=max(20, int(args.min_samples)),
            report_path=args.report_path,
            history_dir=args.history_dir,
            keep_history=not bool(args.no_history),
        )
    except Exception as exc:
        print(f"Hybrid report failed: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=False))
    else:
        print(format_hybrid_report_summary(report, as_json=False))
    return 0 if (report.get("current") or {}).get("ok") else 1


def cmd_source_quality_report(args: argparse.Namespace) -> int:
    windows = [w.strip() for w in str(args.windows).split(",") if w.strip()]
    if not windows:
        windows = ["1h", "24h"]
    try:
        report = generate_source_quality_report(
            config_path=args.config,
            db_path=args.db,
            windows=windows,
            lookback_days=max(1, int(args.lookback_days)),
            min_samples_per_source=max(1, int(args.min_samples_per_source)),
            top_n=max(1, int(args.top_n)),
            report_path=args.report_path,
            history_dir=args.history_dir,
            keep_history=not bool(args.no_history),
        )
    except Exception as exc:
        print(f"Source quality report failed: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=False))
    else:
        print(format_source_quality_summary(report, as_json=False))
    return 0


def cmd_benchmark(args: argparse.Namespace) -> int:
    bench_db = Path(str(args.bench_db))
    if not args.force_db and bench_db.name in {"btc_news_arrow.db", "smoke.db"}:
        print(
            f"Refusing benchmark on risky DB name '{bench_db.name}'. "
            "Use --bench-db with a dedicated filename or pass --force-db.",
            file=sys.stderr,
        )
        return 2
    try:
        summary = run_benchmark(
            db_path=bench_db,
            dedupe_existing=max(50, int(args.dedupe_existing)),
            dedupe_probes=max(50, int(args.dedupe_probes)),
            label_rows=max(100, int(args.label_rows)),
            label_batch_size=max(1, int(args.label_batch_size)),
            keep_db=bool(args.keep_db),
            overwrite_db=bool(args.overwrite_db),
        )
    except Exception as exc:
        print(f"Benchmark failed: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=False))
    else:
        print(format_benchmark_summary(summary, as_json=False))
    return 0


def cmd_regression_check(args: argparse.Namespace) -> int:
    hybrid_windows = [w.strip() for w in str(args.hybrid_windows).split(",") if w.strip()]
    if not hybrid_windows:
        hybrid_windows = ["1h", "24h"]
    try:
        summary = run_regression_check(
            config_path=args.config,
            db_path=args.db,
            baseline_path=args.baseline,
            bench_db_path=args.bench_db,
            dedupe_existing=None if args.dedupe_existing is None else max(50, int(args.dedupe_existing)),
            dedupe_probes=None if args.dedupe_probes is None else max(50, int(args.dedupe_probes)),
            label_rows=None if args.label_rows is None else max(100, int(args.label_rows)),
            label_batch_size=None if args.label_batch_size is None else max(1, int(args.label_batch_size)),
            perf_floor_ratio=float(args.perf_floor_ratio),
            check_hybrid_eval=bool(args.check_hybrid_eval),
            hybrid_windows=hybrid_windows,
            hybrid_lookback_days=max(1, int(args.hybrid_lookback_days)),
            hybrid_min_samples=max(20, int(args.hybrid_min_samples)),
            require_hybrid_samples=bool(args.require_hybrid_samples),
        )
    except Exception as exc:
        print(f"Regression check failed: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=False))
    else:
        print(format_regression_summary(summary, as_json=False))
    return 0 if summary.get("ok") else 1


def cmd_baseline_calibrate(args: argparse.Namespace) -> int:
    hybrid_windows = [w.strip() for w in str(args.hybrid_windows).split(",") if w.strip()]
    if not hybrid_windows:
        hybrid_windows = ["1h", "24h"]
    try:
        summary = calibrate_regression_baseline(
            config_path=args.config,
            db_path=args.db,
            output_path=args.output,
            runs=max(1, int(args.runs)),
            dedupe_existing=max(50, int(args.dedupe_existing)),
            dedupe_probes=max(50, int(args.dedupe_probes)),
            label_rows=max(100, int(args.label_rows)),
            label_batch_size=max(1, int(args.label_batch_size)),
            include_hybrid_eval=bool(args.include_hybrid_eval),
            hybrid_windows=hybrid_windows,
            hybrid_lookback_days=max(1, int(args.hybrid_lookback_days)),
            hybrid_min_samples=max(20, int(args.hybrid_min_samples)),
            hybrid_corr_margin=max(0.0, float(args.hybrid_corr_margin)),
            hybrid_directional_margin=max(0.0, float(args.hybrid_directional_margin)),
            overwrite=bool(args.overwrite),
        )
    except Exception as exc:
        print(f"Baseline calibration failed: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=False))
    else:
        print(format_calibration_summary(summary, as_json=False))
    return 0


def cmd_alerts_check(args: argparse.Namespace) -> int:
    fail_on = {s.strip().lower() for s in str(args.fail_on).split(",") if s.strip()}
    webhook_on = {s.strip().lower() for s in str(args.webhook_on).split(",") if s.strip()}
    try:
        summary = run_alert_checks(
            db_path=args.db,
            hybrid_report_path=args.hybrid_report_path,
            hybrid_history_dir=args.hybrid_history_dir,
            source_quality_report_path=args.source_quality_report_path,
            source_quality_history_dir=args.source_quality_history_dir,
            freshness_minutes=max(1, int(args.freshness_minutes)),
            window_minutes=max(1, int(args.window_minutes)),
            min_items_threshold=max(1, int(args.min_items)),
            source_concentration_threshold=max(0.5, min(1.0, float(args.source_concentration_threshold))),
            hybrid_degraded_streak=max(1, int(args.hybrid_degraded_streak)),
            source_quality_degraded_streak=max(1, int(args.source_quality_degraded_streak)),
            source_quality_corr_drop_threshold=max(0.0, float(args.source_quality_corr_drop_threshold)),
            output_path=args.output_path,
            history_dir=args.history_dir,
            keep_history=not bool(args.no_history),
        )
    except Exception as exc:
        print(f"Alerts check failed: {exc}", file=sys.stderr)
        return 2

    webhook_result = None
    if str(args.webhook_url).strip():
        try:
            webhook_result = send_webhook(
                webhook_url=str(args.webhook_url),
                summary=summary,
                send_on=webhook_on,
                timeout_seconds=max(1, int(args.webhook_timeout)),
                retries=max(0, int(args.webhook_retries)),
                retry_backoff_seconds=max(0.0, float(args.webhook_backoff_seconds)),
                cooldown_seconds=max(0, int(args.webhook_cooldown_seconds)),
                state_path=args.webhook_state_path,
                max_alerts=max(1, int(args.webhook_max_alerts)),
            )
        except Exception as exc:
            webhook_result = {"sent": False, "reason": f"webhook_error: {exc}"}
    if webhook_result is not None:
        summary["webhook"] = webhook_result

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=False))
    else:
        print(format_alert_summary(summary, as_json=False))
        if webhook_result is not None:
            print(f"Webhook: {webhook_result}")

    return 1 if should_fail(summary, fail_on=fail_on) else 0


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        print("Install optional API deps first: pip install .[api]", file=sys.stderr)
        return 2

    uvicorn.run(
        "btc_news_arrow.api:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=False,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "collect":
        return cmd_collect(args)
    if args.command == "rescore":
        return cmd_rescore(args)
    if args.command == "learn-update":
        return cmd_learn_update(args)
    if args.command == "arrow":
        return cmd_arrow(args)
    if args.command == "learn-arrow":
        return cmd_learn_arrow(args)
    if args.command == "llm-arrow":
        return cmd_llm_arrow(args)
    if args.command == "llm-ping":
        return cmd_llm_ping(args)
    if args.command == "llm-optimize":
        return cmd_llm_optimize(args)
    if args.command == "hybrid-optimize":
        return cmd_hybrid_optimize(args)
    if args.command == "hybrid-eval":
        return cmd_hybrid_eval(args)
    if args.command == "hybrid-report":
        return cmd_hybrid_report(args)
    if args.command == "source-quality-report":
        return cmd_source_quality_report(args)
    if args.command == "benchmark":
        return cmd_benchmark(args)
    if args.command == "regression-check":
        return cmd_regression_check(args)
    if args.command == "baseline-calibrate":
        return cmd_baseline_calibrate(args)
    if args.command == "alerts-check":
        return cmd_alerts_check(args)
    if args.command == "forensic":
        return cmd_forensic(args)
    if args.command == "serve":
        return cmd_serve(args)

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

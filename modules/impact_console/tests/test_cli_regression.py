from btc_news_arrow.cli import build_parser


def test_regression_check_benchmark_size_defaults_are_profile_driven():
    parser = build_parser()
    args = parser.parse_args(["regression-check"])

    assert args.dedupe_existing is None
    assert args.dedupe_probes is None
    assert args.label_rows is None
    assert args.label_batch_size is None


def test_hybrid_report_defaults_are_set():
    parser = build_parser()
    args = parser.parse_args(["hybrid-report"])

    assert args.windows == "1h,24h"
    assert args.lookback_days == 30
    assert args.min_samples == 120
    assert args.report_path == "diagnostics/hybrid_eval_latest.json"
    assert args.history_dir == "diagnostics/hybrid_eval_history"
    assert args.no_history is False


def test_alerts_check_defaults_are_set():
    parser = build_parser()
    args = parser.parse_args(["alerts-check"])

    assert args.hybrid_report_path == "diagnostics/hybrid_eval_latest.json"
    assert args.hybrid_history_dir == "diagnostics/hybrid_eval_history"
    assert args.source_quality_report_path == "diagnostics/source_quality_latest.json"
    assert args.source_quality_history_dir == "diagnostics/source_quality_history"
    assert args.freshness_minutes == 30
    assert args.window_minutes == 60
    assert args.min_items == 3
    assert args.source_concentration_threshold == 0.85
    assert args.hybrid_degraded_streak == 2
    assert args.source_quality_degraded_streak == 2
    assert args.source_quality_corr_drop_threshold == 0.05
    assert args.output_path == "diagnostics/alerts_latest.json"
    assert args.history_dir == "diagnostics/alerts_history"
    assert args.no_history is False
    assert args.fail_on == "critical,high,quality"
    assert args.webhook_url == ""
    assert args.webhook_on == "critical,high,quality"
    assert args.webhook_timeout == 5
    assert args.webhook_retries == 2
    assert args.webhook_backoff_seconds == 1.5
    assert args.webhook_cooldown_seconds == 300
    assert args.webhook_state_path == "diagnostics/webhook_state.json"
    assert args.webhook_max_alerts == 12


def test_source_quality_report_defaults_are_set():
    parser = build_parser()
    args = parser.parse_args(["source-quality-report"])

    assert args.windows == "1h,24h"
    assert args.lookback_days == 30
    assert args.min_samples_per_source == 8
    assert args.top_n == 25
    assert args.report_path == "diagnostics/source_quality_latest.json"
    assert args.history_dir == "diagnostics/source_quality_history"
    assert args.no_history is False

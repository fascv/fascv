# btc_news_arrow

Automatischer BTC-News-Impact mit Pfeil-Ausgabe auf Basis kostenloser Feeds.

## Features

- RSS/Atom-Collector (Fed, EZB, SEC, CFTC, BLS inkl. CPI/Employment/PPI/JOLTS, DOL Releases, Exchange-Status)
- Optional GDELT-Ingestion
- Rule-based Kategorie + BTC-Polarity
- Heuristische Verstaerkung fuer News-Qualitaet:
  - Makro-Ueberraschung (z. B. "higher/lower than expected")
  - Regulatorische Finalitaet (finale Entscheidung vs. Entwurf/Kommentar)
  - Incident-Status (Investigating/Monitoring/Resolved)
- Impact-Scoring in `[-1, +1]` mit Source-Weight, Triggern und Zeit-Decay
- Aggregations-Guardrail gegen Quellen-Spam (gleiche Quelle im Fenster wird progressiv gedaempft)
- Story-Cluster-Guardrail gegen Mehrfachmeldungen zur selben News (source-uebergreifend)
- Optional lernendes Online-Modell (korrelationsbasiert) mit BTC-Preis-Labels
- Lernmodell mit erweiterten Feature-Signalen (Quelle/Source-Group, Rule-Scoring-Metadaten, Relevanz-Buckets, Keyword-Hits)
- Label-Guardrail: extreme Returns werden beim Lernen konfigurierbar geclippt
- Optionale Label-Normalisierung (Volatilitaetsanpassung, Deadzone) fuer robustere Lernziele
- Optionaler OpenAI-LLM-Rater als zweiter Schritt (Duplicate-Cluster + Impact JSON)
- Multi-Horizon-Lernen (`5m/15m/1h/24h/1w`) mit gewichteter Mischung pro Ausgabefenster
- Pfeil-Aggregation (Standard: `1h`, `24h`; frei konfigurierbar)
- Forensic-Modus um einen Timestamp (`+-X` Minuten)
- SQLite-Persistenz + Dedupe (GUID/kanonische URL + fuzzy Titel)
- Operational Alerting (`critical/high/quality/drift`) mit optionalem Webhook
- Source-Quality-Report (realisierte Signalqualitaet pro Quelle mit Trendvergleich)
- Dynamische Source-Quality-Gewichtung im Rule-Scoring (aus Report abgeleitet)
- API-Report-Endpunkte (`/reports/*`) fuer Hybrid/Source-Quality/Alerts
- Webhook-Hardening mit Retry/Backoff/Cooldown-State gegen Alert-Spam
- Trading-Signal-Contract (`GET /signal/trading`) fuer Downstream-Module
- Event-Attribution fuer BTC-Moves (news_driven/mixed/no_clear) inkl. Kandidaten-Ranking

## Installation

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

Optional API:

```bash
pip install -e ".[api]"
```

Optional LLM:

```bash
pip install -e ".[llm]"
```

## Konfiguration

Default in `config.yaml`.

Anpassbar:
- `feeds`
- `gdelt`
- `http.user_agent` (wichtig fuer SEC/BLS Fair-Access)
- `source_weights`
- `keyword_rules`
- `heuristics`
- `thresholds`
- `decay`
- `learning`
- `llm`
- `hybrid.score_clip_abs` (begrenzt Ausreisser je Komponente, z. B. `learn`)
- `aggregation.source_repeat_half_life_items` (Diminishing-Returns je weiterer Meldung derselben Quelle)
- `aggregation.cluster_repeat_half_life_items` (Diminishing-Returns je Story-Cluster)
- `source_quality_weighting` (dynamische Quellen-Gewichte aus Source-Quality-Report)
- `learning.model.label_volatility_adjust` + `learning.model.label_deadzone_abs` (robustere Labelziele)
- `attribution` (Preis-Event-Detektion + News-Kandidaten-Attribution)
- `reports` (Dateipfade fuer API-Endpunkte `/reports/*`)
- `alerts` (Default-Schwellen fuer `start.sh`-Alert-Loop inkl. Webhook-Policy)

`start.sh`-Prioritaet fuer Alert-/Report-Defaults:
- `ENV` > `config.yaml` (`alerts`/`reports`) > interne Skript-Defaults
- Optional anderes Config-File: `CONFIG_PATH=/pfad/zu/config.yaml ./start.sh`

## Pflege

- Operativer Pflegeleitfaden: `PFLEGE_PLAN.md`
- Fuer spaetere Iterationen reicht der Auftrag: "optimiere nach Pflegeplan".

GDELT-Hinweise:
- OR-Query in Klammern setzen (z. B. `"(bitcoin OR btc OR cryptocurrency)"`).
- `gdelt.timespan` mindestens `1h` verwenden (zu kurze Fenster werden von GDELT abgewiesen).
- Rate-Limit beachten (ca. 1 Request / 5s). Der Collector nutzt konfigurierbare Retries: `gdelt.max_retries`, `gdelt.retry_seconds`.
- Noise-Guardrails: `gdelt.max_per_domain_per_run`, `gdelt.drop_duplicate_titles`, optional `gdelt.domain_allowlist` / `gdelt.domain_blocklist`.
- Relevanz-Guardrail: `gdelt.require_relevance`, `gdelt.relevance_terms`, `gdelt.min_relevance_matches` (titelbasierte Crypto-Pruefung).

Learn-Stabilitaet:
- `learning.model.max_effective_n` begrenzt Alt-Historie pro Feature (weniger Traegheit)
- `learning.model.feature_recency_half_life_days` gewichtet frische Lernerkenntnisse hoeher
- `learning.model.label_return_clip_abs` begrenzt Label-Ausreisser (z. B. 0.25 = +/-25%)

## CLI

```bash
btcnews collect
btcnews rescore
btcnews learn-update
btcnews arrow
btcnews learn-arrow
btcnews llm-arrow
btcnews llm-ping
btcnews hybrid-optimize
btcnews hybrid-eval
btcnews hybrid-report
btcnews source-quality-report
btcnews alerts-check
btcnews benchmark
btcnews regression-check
btcnews baseline-calibrate
btcnews arrow --window 1h
btcnews forensic --ts "2026-02-06T00:10:00Z" --pm 30m
```

Beispielausgabe:

```text
BTC News Impact (1h): ▲
BTC News Impact (24h): ▬
```

## API (optional)

```bash
btcnews serve --host 0.0.0.0 --port 8000
```

GUI im Browser:

```text
http://127.0.0.1:8000
```

Endpoints:
- `GET /` (Web-GUI)
- `GET /arrow?window=1h&include_reasons=true&reason_limit=3` (Default: `mode=auto`)
- Optional manuell: `GET /arrow?window=1h&mode=auto|rule|learn|blend|llm`
- `POST /collect?include_gdelt=true`
- `POST /learn/update?limit=500`
- `GET /llm/ping`
- `GET /forensic?ts=2026-02-06T00:10:00Z&window=30m`
- `GET /signal/trading?window=1h&mode=auto`
- `GET /signal/attribution?window=1h&limit=5`
- `GET /reports/summary`
- `GET /reports/hybrid?include_history=true&limit=10`
- `GET /reports/source-quality?include_history=true&limit=10`
- `GET /reports/alerts?include_history=true&limit=10`

`GET /arrow` liefert jetzt zusaetzlich:
- `final_score`, `rule_score`, `llm_score`, `learn_score`
- `score_weights_used` (welche Gewichte im Auto-Hybrid effektiv genutzt wurden)
- `signal_state` (`no_signal|neutral|risk_on|risk_off`)
- `attribution_state` (`news_driven|mixed|no_clear_news_driver|not_applicable`)
- `news_driven_probability`, `attribution_event`, `top_attribution`
- `coverage_relevance_sum` plus Mindestgrenzen aus der Hybrid-Logik

`POST /collect` liefert jetzt zusaetzlich:
- `stats.totals` (raw/processed/inserted/dropped/errors)
- `stats.sources` (Breakdown pro Quelle)

LLM-Modus aktivieren:

```bash
export OPENAI_API_KEY=...
# config.yaml: llm.enabled: true
btcnews llm-arrow --window 1h

# Feed-URLs/Reachability testen
./check_feeds.sh
```

GUI-Hinweis:
- Kein Mode-Umschalter mehr. Die GUI nutzt immer `auto`.
- `auto` bedeutet: LLM-first. Standardmaessig startet der Server bei fehlender LLM-Runtime nicht.

LLM ist Pflicht:
- `OPENAI_API_KEY` muss in `.env` gesetzt sein.
- `openai` muss installiert sein (`pip install openai`).
- Bei LLM-Timeout gilt ein kurzer Cooldown pro Fenster (`llm.request_cooldown_seconds`), um Dauerfeuer zu vermeiden.
- Nur neue *materiale* Items triggern standardmaessig neue LLM-Requests (`llm.new_item_min_abs_impact`, Default `0.000001`).
- Optionales Modell-Fallback bei Timeout/Fehlern: `llm.fallback_model`.
- Empfohlen fuer Stabilitaet: `model: gpt-4.1-mini`, `fallback_model: gpt-5-mini`.
- Optional fuer Betrieb ohne LLM-Runtime: `llm.allow_degraded_start: true` (API startet dann und faellt auf Rule-Modus zurueck).

## Regelmaessige Updates

Beispiel (alle 10 Minuten): News sammeln + Regelscore aktualisieren + Lernmodell updaten

```bash
*/10 * * * * cd /home/a/Schreibtisch/codex/Neuer\\ Ordner && .venv/bin/btcnews collect && .venv/bin/btcnews rescore && .venv/bin/btcnews learn-update
```

Ohne Cron: integrierter Loop im Startskript

```bash
cd /home/a/Schreibtisch/codex/Neuer\ Ordner
AUTO_LOOP=1 UPDATE_INTERVAL_SECONDS=600 ./start.sh
```

Automatisches Hybrid-Re-Fitting (standardmaessig an):
- Startet einmal beim Boot und danach periodisch.
- Default-Intervall: `HYBRID_OPT_INTERVAL_SECONDS=21600` (6h).
- Relevante Variablen: `AUTO_HYBRID_OPTIMIZE`, `HYBRID_OPT_WINDOWS`, `HYBRID_OPT_LOOKBACK_DAYS`, `HYBRID_OPT_MIN_SAMPLES`, `HYBRID_OPT_GRID_STEP`.

Automatische Baseline-Rekalibrierung (standardmaessig an):
- Startet einmal beim Boot und danach periodisch.
- Default-Intervall: `BASELINE_CAL_INTERVAL_SECONDS=86400` (24h).
- Relevante Variablen: `AUTO_BASELINE_CALIBRATE`, `BASELINE_CAL_OUTPUT`, `BASELINE_CAL_RUNS`, `BASELINE_CAL_INCLUDE_HYBRID_EVAL`, `BASELINE_CAL_WINDOWS`, `BASELINE_CAL_LOOKBACK_DAYS`, `BASELINE_CAL_MIN_SAMPLES`, `BASELINE_CAL_CORR_MARGIN`, `BASELINE_CAL_DIRECTIONAL_MARGIN`.

Automatischer Hybrid-Eval-Trendreport (standardmaessig an):
- Startet einmal beim Boot und danach periodisch.
- Default-Intervall: `HYBRID_EVAL_INTERVAL_SECONDS=21600` (6h).
- Schreibt `HYBRID_EVAL_REPORT_PATH` plus optionale History-Snapshots in `HYBRID_EVAL_HISTORY_DIR`.
- Relevante Variablen: `AUTO_HYBRID_EVAL_REPORT`, `HYBRID_EVAL_WINDOWS`, `HYBRID_EVAL_LOOKBACK_DAYS`, `HYBRID_EVAL_MIN_SAMPLES`, `HYBRID_EVAL_KEEP_HISTORY`.

Automatischer Source-Quality-Report (standardmaessig an):
- Startet einmal beim Boot und danach periodisch.
- Default-Intervall: `SOURCE_QUALITY_INTERVAL_SECONDS=21600` (6h).
- Schreibt `SOURCE_QUALITY_REPORT_PATH` plus optionale History in `SOURCE_QUALITY_HISTORY_DIR`.
- Relevante Variablen: `AUTO_SOURCE_QUALITY_REPORT`, `SOURCE_QUALITY_WINDOWS`, `SOURCE_QUALITY_LOOKBACK_DAYS`, `SOURCE_QUALITY_MIN_SAMPLES_PER_SOURCE`, `SOURCE_QUALITY_TOP_N`, `SOURCE_QUALITY_KEEP_HISTORY`.

Automatischer Alert-Check (standardmaessig an):
- Startet einmal beim Boot und danach periodisch.
- Default-Intervall: `ALERT_INTERVAL_SECONDS=600` (10m).
- Schweregrade: `critical`, `high`, `quality`, `drift`.
- Relevante Variablen: `AUTO_ALERT_CHECK`, `ALERT_FRESHNESS_MINUTES`, `ALERT_WINDOW_MINUTES`, `ALERT_MIN_ITEMS`, `ALERT_SOURCE_CONCENTRATION_THRESHOLD`, `ALERT_HYBRID_DEGRADED_STREAK`, `ALERT_SOURCE_QUALITY_DEGRADED_STREAK`, `ALERT_SOURCE_QUALITY_CORR_DROP_THRESHOLD`, `ALERT_FAIL_ON`.
- Optional Webhook: `ALERT_WEBHOOK_URL`, `ALERT_WEBHOOK_ON`, `ALERT_WEBHOOK_TIMEOUT`, `ALERT_WEBHOOK_RETRIES`, `ALERT_WEBHOOK_BACKOFF_SECONDS`, `ALERT_WEBHOOK_COOLDOWN_SECONDS`, `ALERT_WEBHOOK_STATE_PATH`, `ALERT_WEBHOOK_MAX_ALERTS`.
- Output: `ALERT_OUTPUT_PATH` plus optionale History in `ALERT_HISTORY_DIR` (`ALERT_KEEP_HISTORY`).

Optional: LLM-Auto-Optimizer beim Start ausfuehren (nutzt echte LLM-Responses und passt Parameter vorsichtig an):

```bash
AUTO_LLM_OPTIMIZE=1 LLM_OPT_CYCLES=1 LLM_OPT_WINDOWS=1h,24h ./start.sh
```

Hybrid-Gewichte datengetrieben auf Kurskorrelation fitten:

```bash
btcnews hybrid-optimize --windows 1h,24h --lookback-days 30 --min-samples 120
```

Rule/Learn/Hybrid-Qualitaet auf historischen Labels auswerten:

```bash
btcnews hybrid-eval --windows 1h,24h --lookback-days 30 --min-samples 120
```

`hybrid-eval` enthaelt jetzt zusaetzlich Walk-Forward/OOS-Metriken inkl. tradingnaher Kennzahlen
(`metrics.walk_forward.*`), um Leakage-Risiken zu reduzieren.

Hybrid-Eval mit Trendvergleich gegen den vorherigen Lauf speichern:

```bash
btcnews hybrid-report --windows 1h,24h --lookback-days 30 --min-samples 120
# JSON-Ausgabe + persistenter Report:
btcnews hybrid-report --json --report-path diagnostics/hybrid_eval_latest.json
```

Operationalen Alert-Check ausfuehren:

```bash
btcnews alerts-check
# nur auf critical/high failen:
btcnews alerts-check --fail-on critical,high
# mit Webhook:
btcnews alerts-check --webhook-url https://example.com/hook --webhook-on critical,high,quality
# inklusive Source-Quality-Checks:
btcnews alerts-check --source-quality-report-path diagnostics/source_quality_latest.json --source-quality-degraded-streak 2 --source-quality-corr-drop-threshold 0.05
# robuster Webhook (Retry + Cooldown):
btcnews alerts-check --webhook-url https://example.com/hook --webhook-retries 3 --webhook-backoff-seconds 2 --webhook-cooldown-seconds 300
```

Signalqualitaet pro Quelle reporten:

```bash
btcnews source-quality-report --windows 1h,24h --lookback-days 30 --min-samples-per-source 8
# JSON + persistenter Report:
btcnews source-quality-report --json --report-path diagnostics/source_quality_latest.json
```

Lokalen Throughput-Benchmark fuer Dedupe + Label-Upserts laufen lassen:

```bash
btcnews benchmark --bench-db bench.db --dedupe-existing 5000 --dedupe-probes 2000 --label-rows 20000 --label-batch-size 1000
# Vollausgabe als JSON:
btcnews benchmark --bench-db bench.db --json
# Existierende Benchmark-DB bewusst ueberschreiben:
btcnews benchmark --bench-db bench.db --overwrite-db
```

CI-/Regressions-Gate gegen Baseline laufen lassen:

```bash
btcnews regression-check --baseline tools/regression_baseline.json
# mit optionalem Hybrid-Eval-Gate (falls genug gelabelte Samples vorhanden):
btcnews regression-check --check-hybrid-eval --hybrid-windows 1h,24h
# Benchmark-Groessen werden ohne Flags aus baseline.benchmark_profile gelesen.
# Ungueltige/missing Performance-Baselinewerte fuehren zu einem klaren Fail.
```

Baseline datengetrieben kalibrieren (3-5 Runs empfohlen):

```bash
btcnews baseline-calibrate --output tools/regression_baseline.json --runs 5 --overwrite
# optional Hybrid-Floors aus aktuellen Labels ableiten:
btcnews baseline-calibrate --include-hybrid-eval --hybrid-windows 1h,24h --overwrite
# Schreibt neben Performance-Floors auch baseline.benchmark_profile.
```

CI:
- Workflow-Datei: `.github/workflows/regression-check.yml`
- Fuehrt `pytest -q` und danach `btcnews regression-check` aus.
- Nightly-Report-Workflow: `.github/workflows/nightly-reports.yml` (erstellt Diagnostics-Reports und laedt Artifacts hoch).

Optimizer-Guardrails (in `hybrid.optimizer`):
- Optionaler Holdout-Check (`holdout_ratio`, `min_holdout_samples`, `require_holdout_improvement`)
- Mindestverbesserungen (`min_train_improvement`, `min_holdout_improvement`)
- Begrenzte Gewichtsverschiebung pro Lauf (`max_share_shift`)

Hinweis: `start.sh` weicht automatisch auf den naechsten freien Port aus, falls der konfigurierte Port bereits belegt ist.

## Tests

```bash
pip install -e ".[dev]"
pytest
```

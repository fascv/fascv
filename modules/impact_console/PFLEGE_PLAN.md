# Pflegeplan fuer BTC News Modul

Diese Datei ist die operative Referenz fuer manuelle Nachschaerfung.
Wenn du spaeter sagst: "Schau in die Datei und optimiere unter den aktuellen Bedingungen",
dann ist genau dieser Ablauf gemeint.

## Ziel

- News-Impact fuer BTC robust halten.
- Falsche Impacts reduzieren.
- Neue Markt-Narrative schnell aufnehmen.
- Kosten/Nutzen von LLM sinnvoll halten.

## Wartungsrhythmus

1. Woechentlich (15-30 Minuten)
- Source-Quality pruefen (Top/Flop Quellen, 1h und 24h).
- Letzte 7 Tage auf Fehlklassifikationen pruefen.
- Kleine Keyword-Fixes (nur additive oder sehr gezielte Korrekturen).

2. Alle 2 Wochen (30-60 Minuten)
- Kategorien und Polarity-Woerter nachschaerfen.
- Neue Narrative einbauen (Regulierung, ETF-Flows, Security-Incidents, Makro-Ueberraschungen).
- Tote oder verrauschende Keywords entfernen.

3. Monatlich (60-90 Minuten)
- Schwellenwerte und Heuristiken gegen letzte 30 Tage pruefen.
- Attribution-Schwellen validieren (`news_driven`/`mixed`/`no_clear`).
- LLM-Prompt/Schema nur bei systematischer Fehlbewertung anpassen.

4. Sofort (ausserplanmaessig) bei Triggern
- Laengere 0-Impact-Phasen trotz hohem News-Durchsatz.
- Deutlicher Qualitaetsabfall in `source_quality` (korrelation/directional).
- Neue Ereignisarten im Markt, die nicht sauber klassifiziert werden.

## Trigger fuer direkte Nachschaerfung

- `source_quality` Trend wiederholt `degraded`.
- 1h `dir_acc < 0.48` oder 24h `dir_acc < 0.53` ueber mehrere Runs.
- Groessere Drift zwischen starker BTC-Bewegung und `attribution_state=no_clear_news_driver`.
- Viele LLM-Aufrufe ohne neuen Material-Nutzen.

## Standardablauf "Jetzt optimieren"

1. Ist-Zustand ziehen

```bash
.venv/bin/btcnews source-quality-report --windows 1h,24h --lookback-days 30 --min-samples-per-source 8
.venv/bin/btcnews hybrid-report --windows 1h,24h --lookback-days 30 --min-samples 120
.venv/bin/btcnews alerts-check --fail-on critical,high
```

2. Live-Signal und Attribution pruefen

```bash
curl -s "http://127.0.0.1:8000/signal/trading?window=1h&mode=auto"
curl -s "http://127.0.0.1:8000/signal/attribution?window=1h&limit=5"
curl -s "http://127.0.0.1:8000/signal/attribution?window=24h&limit=5"
```

3. Datenhygiene pruefen

```bash
sqlite3 btc_news_arrow.db "select count(*) from items where julianday(timestamp_utc) > julianday('now') + (5.0/(24*60));"
sqlite3 btc_news_arrow.db "select source, count(*) as n from items where julianday(timestamp_utc) >= julianday('now','-24 hours') group by source order by n desc limit 15;"
```

4. Nur noetige Anpassungen durchfuehren
- `config.yaml`: `keyword_rules`, `heuristics`, ggf. `thresholds`.
- Keine grossen Umbauten ohne klaren Messgewinn.

5. Recompute und Kontrolle

```bash
.venv/bin/btcnews rescore
.venv/bin/btcnews learn-update
.venv/bin/btcnews source-quality-report --windows 1h,24h --lookback-days 30 --min-samples-per-source 8
.venv/bin/pytest -q
```

## Konkrete Optimierungshebel

1. Keywords/Kategorien
- Immer mit Varianten denken (`outflow/outflows`, `loss/losses`, `probe/probes`).
- Erst praezise neue Begriffe aufnehmen, dann ggf. erweitern.
- Bei Mehrdeutigkeit lieber ueber Kontext-Keywords absichern.

2. Polarity
- Negative Incident-Begriffe strikt negativ halten (z. B. `breach`, `hack`, `outage`, `lawsuit`).
- Positive Begriffe nur dort nutzen, wo Marktreaktion stabil positiv ist.

3. Source-Qualitaet
- Automatische Gewichtung laeuft bereits.
- Manuelle Eingriffe nur bei dauerhaftem Muster und ausreichender Stichprobe.
- Einzelausreisser nie uebersteuern.

4. Attribution
- Ziel ist Erklaerbarkeit, nicht Kausalbeweis.
- Bei vielen `no_clear` trotz hoher Volatilitaet: Event-/Candidate-Schwellen pruefen.
- Bei zu vielen `news_driven` bei Mikromeldungen: Top-Score/Probability-Schwellen anheben.

## Was nicht dauernd angefasst werden sollte

- LLM-Parameter taeglich drehen.
- Grosse Category-Rewrites ohne Evaluierung.
- Schwellenwerte mehrfach am selben Tag aendern.

## Erfolgskriterien nach einer Nachschaerfung

- Mehr plausible non-zero Impacts bei relevanten News.
- Bessere Stabilitaet in `dir_acc` (1h/24h).
- Weniger offensichtliche False-Positives durch kleine Exchange-Statusmeldungen.
- Attribution liefert haeufiger sinnvolle Top-Kandidaten bei echten Moves.

## Kurzkommando fuer spaeter

Wenn du "optimiere nach Pflegeplan" sagst, wird folgender Ablauf erwartet:

1. Aktuelle Reports/Alerts ziehen.
2. Schwachstellen identifizieren (Quellen, Keywords, Schwellen).
3. Minimal-invasive Anpassungen in `config.yaml`/Regeln.
4. `rescore`, `learn-update`, Report neu erzeugen.
5. Ergebnis gegen vorher vergleichen und kurz dokumentieren.

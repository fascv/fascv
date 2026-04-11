# Exec Runbook (Kraken Spot)

Dieses Runbook beschreibt den Standard-Ablauf, um das `exec`-Modul reproduzierbar zu testen und manuell zu prüfen.

## 1. Preflight

Im Repo-Root:

```bash
cd /home/a/Schreibtisch/codex/bitcoin
python3 -m unittest tests.test_exec_restart_recovery tests.test_owntrades_dedupe tests.test_rate_limit tests.test_deadman tests.test_state_machine -v
python3 scripts/chaos_harness.py --config configs/chaos.yaml
```

Erwartung:
- Unit-Tests: `OK`
- Chaos-Harness: `PASS`

## 2. Start (Paper)

```bash
cd /home/a/Schreibtisch/codex/bitcoin
python3 -m trading.launch --mode paper --config configs/chaos.yaml --pidfile logs/manual_pids.json
```

Hinweis:
- Wenn `control.port` belegt ist, einen freien Port in einer Runtime-Config setzen (wie im Harness) oder den blockierenden Prozess beenden.

## 3. Exec-GUI starten

In einem zweiten Terminal:

```bash
cd /home/a/Schreibtisch/codex/bitcoin/modules/exec
EXEC_GUI_CONTROL_URL=http://127.0.0.1:8100 ./start_exec_gui.sh
```

GUI:
- `http://127.0.0.1:8110/`

## 4. GUI-Sollbild

Im normalen Paper-Betrieb:
- `Mode = paper`
- `Open Orders` meist `0`
- `Rate Limited = false` (kurze Peaks sind ok)
- keine dauerhafte `fill_truth_gap`-Warnung
- kein dauerhafter Deadman-Fehler

## 5. Manueller PAUSE/RESUME-Check

```bash
curl -X POST http://127.0.0.1:8100/pause
curl -X POST http://127.0.0.1:8100/resume
```

Erwartung:
- `core.trading_enabled`: `true -> false -> true`
- Journal zeigt `deadman disable` und nach Resume wieder `deadman tick`

## 6. Live-Smoketest (optional)

Nur mit gesetzten Kraken-Credentials:

```bash
cd /home/a/Schreibtisch/codex/bitcoin
python3 -m trading.launch --mode live --config configs/live.yaml --pidfile logs/live_pids.json
```

Zusätzlich prüfen:
- keine auth/rate-limit Fehlerflut im Journal
- `fill`-Events kommen aus `ownTrades`
- keine inkonsistenten offenen Orders nach Restart

## 7. Shutdown

Im Launch-Terminal `Ctrl+C` drücken.

Optional aufräumen:

```bash
pkill -f "python3 -m trading.launch" || true
pkill -f "python3 -m trading_exec.gui" || true
```

## 8. Schnell-Troubleshooting

- `address already in use`:
  - Port belegt, freien Port wählen.
- `deadman_disabled_while_trading` in Paper:
  - sollte mit aktueller GUI-Heuristik nur noch bei echten Auffälligkeiten erscheinen.
- `rate_limit_unbalanced`:
  - Hinweis auf mehr Pause- als Resume-Events im sichtbaren Journal-Fenster; Fenster vergrößern/History prüfen.

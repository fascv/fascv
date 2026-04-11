# exec module

Scope:
- order placement/cancel/reconcile
- state machine and idempotency
- rate-limit and deadman handling

Primary runtime implementation remains in `trading/processes/exec.py`.
Workspace API: `modules/exec/src/trading_exec/process.py`.

## Exec-Only GUI

Dieses Modul enthält eine dedizierte GUI für Exec-Observability:

- zeigt nur exec-relevante Telemetrie/Journal-Ereignisse
- baut Order-Lifecycle-Ansicht (`exec_report` + `fill`)
- markiert einfache Plausibilitätswarnungen (z. B. Status-Regressionen, unbalancierte Rate-Limit-Pause/Resume)

Start (aus Repo-Root):

```bash
PYTHONPATH=modules/exec/src python3 -m trading_exec.gui --control-url http://127.0.0.1:8100 --port 8110
```

Dann im Browser öffnen:

- `http://127.0.0.1:8110/`

Starter direkt im Modulverzeichnis:

```bash
./start_exec_gui.sh
```

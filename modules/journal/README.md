# journal module

Scope:
- persistence and event journal
- sqlite/json writers

Primary runtime implementation remains in `trading/processes/journal.py`.
Workspace API: `modules/journal/src/trading_journal/process.py`.

## Journal-Only GUI

Dieses Modul enthaelt eine dedizierte GUI fuer Journal-Persistence/Integrity:

- Tail + Filter fuer `journal_events.jsonl` (inkl. Parse-Error-Zaehler)
- Checks: newline-termination, Rotation-Files, file sizes
- SQLite: Schema/Indexes + letzte Rows aus `events`

Start (aus Repo-Root):

```bash
PYTHONPATH=modules/journal/src python3 -m trading_journal.gui --json-path logs/journal_events.jsonl --db-path logs/journal.db --port 8120
```

Oder direkt im Modul:

```bash
./start_journal_gui.sh
```

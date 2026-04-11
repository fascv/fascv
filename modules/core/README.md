# core module

Scope:
- feature computation
- alpha + cost + gate + risk decisions
- order intent generation

Primary runtime implementation remains in `trading/processes/core.py`.
Workspace API: `modules/core/src/trading_core/process.py`.

## Core-Only GUI

Dieses Modul enthaelt eine dedizierte GUI fuer Core-Decision-Observability.
Sie liest *read-only* aus der Control-API (`/status` + `/journal`) und kann spaeter leicht per iframe in die Control-GUI eingebettet werden.

Start (aus Repo-Root):

```bash
PYTHONPATH=modules/core/src python3 -m trading_core.gui --control-url http://127.0.0.1:8100 --port 8130
```

Oder direkt im Modul:

```bash
./start_core_gui.sh
```

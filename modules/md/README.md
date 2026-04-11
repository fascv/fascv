# md module

Scope:
- Kraken/public market data ingestion
- paper replay market feed
- stale/reconnect/checksum handling

Primary runtime implementation remains in `trading/processes/md.py`.
Workspace API: `modules/md/src/trading_md/process.py`.

GUI:
- Standalone read-only UI (consumes Control API `/status` + `/journal`):
  - `modules/md/start_md_gui.sh`

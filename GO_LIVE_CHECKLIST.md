# GO_LIVE_CHECKLIST

## Ziel
Live-Trading auf Kraken Spot sicher und zuverlässig betreiben.
Priorität: Sicherheit > Stabilität > Latenz > Optimierung.

## 0) Grundannahmen / Modes
- `paper`: keine echten Orders
- `live + canary_mode`: `validate=true` (keine `txid` erwartet, Status `VALIDATED`)
- `live + armed`: echte Orders erlaubt

## 1) Host-Preflight (einmalig)
### System / Ressourcen
- [ ] Wired Ethernet (kein WLAN) wenn möglich
- [ ] Zeit synchron (chrony/ntpd aktiv)
- [ ] `/dev/shm` vorhanden und nicht zu klein:
- `df -h /dev/shm`
- `mount | grep shm`
- [ ] ulimits:
- [ ] `ulimit -n` >= 65535 (empfohlen)
- [ ] `ulimit -u` nicht zu klein

### Benutzer / Rechte
- [ ] Dedizierter Linux-User (z.B. `trader`, nicht `root`)
- [ ] Repo/WorkingDir z.B. `/opt/btc-trader` gehört `trader`

## 2) Secrets / API Keys
- [ ] Kraken API-Key: keine Withdrawal-Rechte, nur benötigte Trading/Query-Rechte
- [ ] `.env` liegt außerhalb von Git
- [ ] `.env` hat `chmod 600`
- [ ] Keys erscheinen nie in Logs/Journal/Telemetry

Beispiel `.env`:
- `KRAKEN_API_KEY=...`
- `KRAKEN_API_SECRET=...`

## 3) Konfig-Defaults (Start konservativ)
### Stale-Schwellen
- [ ] `md.stale_seconds: 10`
- [ ] `md.stale_book_seconds: 5`
- [ ] `md.stale_trade_seconds: 15`

### Deadman
- [ ] `exec.deadman_timeout_sec: 60`
- [ ] `exec.deadman_tick_sec: 20`
- [ ] Deadman-Tick läuft im gleichen Token-Bucket-Budget wie REST-Orders

### Rate-Limit Verhalten
- [ ] `core.rate_limit_pause_sec: 30` (oder höher)
- [ ] Token-Bucket so dimensionieren, dass Deadman + Orders stabil laufen

### Risk Limits (erste Armed-Phase sehr klein)
- [ ] `max_exposure_eur`: 20-50 EUR
- [ ] `max_orders_per_min`: 2-6
- [ ] `daily_loss_limit_eur`: 2-5 EUR
- [ ] Cooldown nach Verlust-Trades aktiv
- [ ] Optional Start nur mit `post_only` (Maker)

## 4) Build/Tests (vor jedem Deployment)
- [ ] `python3 -m unittest discover -s tests` ist grün
- [ ] `python3 -m trading.launch --mode paper --config configs/paper.yaml` startet sauber
- [ ] Journal/Logs werden geschrieben
- [ ] Control-Plane (falls aktiv) liefert `/health` = ok

## 5) Chaos Harness (Pflicht vor Live)
- [ ] `python3 scripts/chaos_harness.py --config configs/chaos.yaml` ergibt `PASS`

Erwartete Kernchecks:
- [ ] `md` kill -> `core` STOP -> `exec` CANCEL_ALL -> deadman `timeout=0`
- [ ] `exec` kill -> `core` PAUSE/STOP -> Restart -> Reconcile ok
- [ ] WS disconnect -> reconnect/resubscribe -> keine Endlosschleife
- [ ] checksum mismatch -> resync -> book konsistent
- [ ] rate-limit -> `core` PAUSE -> Auto-Resume

Artefakte prüfen:
- [ ] Artifacts-Log vorhanden
- [ ] Journal enthält STOP/CANCEL_ALL + deadman-disable Events

## 6) Live Canary (`validate=true`) für mehrere Stunden
Konfig:
- [ ] `exec.canary_mode: true`

Start:
- [ ] `python3 -m trading.launch --mode live --config configs/live.yaml`

Erfolgskriterien:
- [ ] Keine stale-stops im Normalbetrieb
- [ ] WS reconnects selten und sauber
- [ ] Rate-limit Hits niedrig; bei Treffer PAUSE/Auto-Resume korrekt
- [ ] Deadman tickt stabil; bei PAUSE/STOP wird `timeout=0` gesendet
- [ ] `VALIDATED` Responses kommen zuverlässig
- [ ] Telemetry Queue-Depths sind stabil

## 7) Live Armed (echte Orders) mit Minimal-Einsatz
Konfig:
- [ ] `exec.canary_mode: false`
- [ ] Risk-Limits bleiben konservativ

Start:
- [ ] Engine starten, zunächst 1-2 Trades eng beobachten

Erfolgskriterien (erste 24h):
- [ ] CancelAll + Deadman funktionieren im Ernstfall (STOP-Test)
- [ ] ownTrades fills korrekt, dedupe greift
- [ ] Realized fees/slippage plausibel
- [ ] Restart-Recovery rekonstruiert offene Orders sauber

## 8) Betrieb als Service (empfohlen)
- [ ] Engine läuft als `systemd` Service (nicht in VNC)
- [ ] Auto-Restart aktiv
- [ ] Logs über `journalctl` und optional `logrotate`

Referenz:
- `ops/btc-trader.service`
- `ops/btc-trader.logrotate`

## 9) Incident Playbook (Kurz)
Bei auffälligem Verhalten:
1. STOP (`core` stop trading)
2. CANCEL_ALL + Deadman disable (`timeout=0`)
3. Status prüfen: openOrders, ownTrades, Journal
4. Ursache klären: stale, reconnect-loop, rate-limit, checksum mismatch
5. Erst danach wieder `resume`/`armed`

Optional:
- Flatten Position (wenn implementiert)

## 10) Change Management
- [ ] Änderungen an `configs/live.yaml` versionieren (Tag/Release)
- [ ] Reload nur an Safe-Points (keine offenen Orders) oder via Control Command
- [ ] Nach Deploy: Tests + paper smoke + ggf. kurze Canary-Phase

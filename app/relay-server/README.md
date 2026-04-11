# Binance Relay + Web Dashboard (Trading-PC)

Der Relay laeuft auf deinem Trading-PC (mit Binance API Key/Secret).
Du greifst direkt per Browser ueber Tailscale darauf zu.

## Setup

1. Environment vorbereiten:
```bash
cd relay-server
cp .env.example .env
# .env bearbeiten
```

Pflichtvariablen:
- `BINANCE_API_KEY`
- `BINANCE_API_SECRET`
- `RELAY_TOKEN`

Optional:
- `BINANCE_BASE_URL` (Default `https://api.binance.com`)
- `RELAY_HOST` (Default `0.0.0.0`)
- `RELAY_PORT` (Default `8000`, empfohlen `8940`)
- `ROTATION_ACTIVE_FILE` (Default: `../../bitcoin/configs/rotation_active_lanes.json`)

2. Start als Service:
```bash
systemctl --user daemon-reload
systemctl --user enable --now binance-relay.service
systemctl --user status binance-relay.service
```

3. Browser aufrufen:
```text
http://<TAILSCALE-IP>:<PORT>/
```
Beispiel:
```text
http://100.67.151.96:8940/
```

## Dashboard

Im Browser-Dashboard gibst du ein:
- `Relay Token`
- `Tag` im Kalender

Dann bekommst du:
- Chronologische Buy/Sell-Buendel
- Summen pro Symbol
- Gesamtzusammenfassung fuer den Zeitraum
- Rotation Punkte 1/2/3 je Coin (inkl. Gate-Reason + post-dump-Status)

Hinweis:
- Teilfills derselben Binance-Order (`orderId`) werden automatisch zusammengefuehrt, damit ein Kauf/Verkauf nicht mehrfach als einzelne Mini-Posten erscheint.

## API Endpoints

- `GET /health`
- `POST /trades/report` (Bearer Token)
- `GET /symbols/usdc` (Bearer Token, optional Debug)
- `GET /rotation/points` (Bearer Token)

`POST /trades/report` Beispiel:
```json
{
  "dayUtc": "2026-03-05"
}
```

## Sicherheit

- Zugriff nur ueber Tailscale erlauben.
- `RELAY_TOKEN` lang und zufaellig waehlen.
- Binance API Key nur mit `Enable Reading` betreiben.

# Rotation Tuning Log 2026-03-04

## Zweck

Dieses Protokoll fixiert den Stand fuer den 4. Maerz 2026, damit die
Rotation morgen gegen einen klaren Referenzpunkt bewertet werden kann.

Es dokumentiert:

- den Ausgangszustand vor dem heutigen groesseren Optimierungsblock
- die heute ausgewerteten Live-Trades
- die daraus abgeleiteten Schwachstellen
- die danach gesetzten Parameter

Wichtig:

- Nicht jeder bereits laufende Trader hat diese Werte sofort live.
- Bereits laufende Prozesse uebernehmen neue Basiswerte erst beim naechsten
  Restart oder Rotationswechsel.


## Referenz-Zeitpunkte

- `100 bps`-Aenderung fuer `failed_start_loss_bps`:
  - Proxy-Zeitpunkt via MTime von
    `scripts/rotation_auto_coin_selector.py`
  - `2026-03-04 11:28:25 UTC`
- Die nachfolgende Trade-Auswertung verwendet diesen Zeitpunkt als Schnitt.


## Urzustand Vor Dem Letzten Optimierungsblock

Das war der wesentliche Stand vor der letzten, datenbasierten Nachschaerfung:

- Pool:
  - auf `50` Coins erweitert
- Alpha:
  - `alpha.type: continuation`
- Rotation-Stickiness:
  - `min_active_minutes: 30`
  - `active_retain_min_score: 0`
  - `max_retain_position_pct: 100`
- Selector:
  - `fresh_bottom` war noch weniger strikt
  - fruehe Rebounds wurden spaeter erkannt
  - spaetere Rebounds wurden erst spaeter ausgeschlossen
- Continuation-Defaults:
  - `trend_min_bps: 24.0`
  - `recent_bias_min_bps: 0.0`
  - `max_chase_bps: 26.0`
  - `max_range_pos: 0.82`
- Exit-Basis:
  - `hard_stop_loss_bps: 200.0`
  - `min_exit_profit_bps: 0.0`
  - `time_break_even_floor_enabled: false`
  - `trailing_activation_bps: 8.0`
  - `trailing_stop_bps: 8.0`
  - `trailing_stop_atr_mult: 0.5`
  - `failed_start_max_bars: 2`
  - `failed_start_min_rebound_bps: 12.0`
  - `failed_start_loss_bps: 100.0`
  - `red_candle_exit_enabled: false`


## Trades Heute Nach Dem 100-Bps-Schnitt

Ausgewertet wurden gepaarte Buy/Sell-Trades nach `2026-03-04 11:28:25 UTC`.

Gesamtbild:

- `13` gepaarte Trades
- `10` brutto positive Trades
- `3` brutto negative Trades
- aber nur `3` netto positive Trades
- `10` netto negative Trades
- Netto-Summe: ca. `-0.605 EUR`

Kernaussage:

- Die Mehrzahl der Trades war brutto leicht im Plus.
- Nach Gebuehren waren diese Gewinne oft zu klein.
- Die groesseren Verluste kamen aus einzelnen laenger laufenden Fehltrades,
  die bis zum harten Stop gelaufen sind.

### Auffaellige Einzelbeispiele

- `ARB`
  - `+29.1 bps` brutto
  - Exit `trailing_stop`
  - netto negativ
  - Signal: Gewinnmitnahme war technisch ok, aber brutto zu klein fuer den
    Roundtrip

- `FET` (2 Trades)
  - beide etwa `+26 bps` brutto
  - beide `trailing_stop`
  - beide netto negativ
  - Signal: kleine Rebounds wurden zwar mitgenommen, aber nicht weit genug

- `ENA` (Trade 2)
  - `+17.2 bps` brutto
  - Exit `trailing_stop`
  - netto negativ
  - Signal: gleiches Problem wie oben

- `DOT` (Trade 1)
  - `-207 bps`
  - Exit `hard_stop_loss`
  - klarer Ausreisser-Verlust

- `MORPHO`
  - `-216.7 bps`
  - Exit `hard_stop_loss`
  - zweiter klarer Ausreisser-Verlust

- `DOT` (Trade 4)
  - `+78.4 bps` brutto
  - Exit `trailing_stop`
  - netto positiv
  - Signal: wenn wirklich Strecke da ist, funktioniert der Exit

- `INJ`
  - `+72.0 bps` brutto
  - Exit `trailing_stop`
  - netto positiv

- `TIA`
  - `+47.7 bps` brutto
  - Exit `trailing_stop`
  - leicht netto positiv


## Ableitung Aus Den Heutigen Daten

Die groessten Probleme waren:

- Rotation kam oft noch zu spaet in den Rebound.
- Dadurch blieb bis zum Peak haeufig zu wenig Strecke.
- Viele `trailing_stop`-Exits waren nicht zu spaet, sondern zu klein.
- Einzelne Fehltrades wurden zu lange gehalten und erst am grossen Stop
  beendet.

Daraus folgt:

- frischer und tiefer selektieren
- oben schneller aus der aktiven Auswahl herausfallen
- Gewinn nur mit echtem Nettopuffer mitnehmen
- Totlaeufer frueher als "nicht bestaetigt" beenden
- grossen Katastrophenstop enger setzen


## Neuer Stand Nach Der Nachschaerfung

### Rotation

In `scripts/rotation_auto_coin_selector.py`:

- `min_active_minutes: 30` bleibt
- aber freie Slots bleiben nicht mehr blind kleben
- `active_retain_min_score: 80.0`
- `max_retain_position_pct: 62.0`

Wirkung:

- Ein Coin darf innerhalb der Haltezeit trotzdem rausfallen,
  wenn er schon zu hoch in seiner lokalen Range steht
  oder sein Score nicht mehr gut genug ist.

### Selector

In `scripts/select_rotation_watchlist.py`:

- `fresh_bottom` strenger:
  - nur noch sehr frische lokale Tiefs
- Rebound-Freigabe frueher:
  - kleinere frische Gegenbewegung reicht
- `too_late_rebound` deutlich strenger:
  - spaeter hochgelaufene Rebounds fliegen frueher raus

Konkret:

- `fresh_bottom`
  - vorher: lockerer
  - jetzt: `bars_since_swing_low <= 2` oder `bars_since_30m_low <= 1`
- `recent_rebound_ready`
  - jetzt schon ab:
    - `ret10_bps >= 2.0`
    - `rebound_from_30m_low_bps >= 4.0`
- `too_late_rebound`
  - deutlich frueher aktiv
  - obere lokale Range wird frueher ausgeschlossen

### Entry-Alpha

In `trading/alpha/factory.py` fuer `continuation`:

- `trend_min_bps: 16.0`
- `recent_bias_min_bps: 6.0`
- `max_chase_bps: 16.0`
- `max_range_pos: 0.68`

Wirkung:

- etwas frueherer Einstieg bei frischer Bodenaufnahme
- gleichzeitig weniger Kauf in bereits gelaufene kleine Rebounds

### Exit-Logik

Zentral in `scripts/rotation_auto_coin_selector.py` und statisch in allen
Pool-Configs:

- `hard_stop_loss_bps: 120.0`
- `min_exit_profit_bps: 20.0`
- `time_break_even_floor_enabled: true`
- `time_break_even_floor_bars: 12`
- `trailing_activation_bps: 24.0`
- `trailing_stop_bps: 6.0`
- `trailing_stop_atr_mult: 0.35`
- `failed_start_max_bars: 4`
- `failed_start_min_rebound_bps: 25.0`
- `failed_start_loss_bps: 60.0`

Interpretation:

- Kleine brute Gewinne unter echtem Kostenpuffer sollen seltener realisiert
  werden.
- Reale Gewinner duerfen etwas weiter laufen, muessen dann aber frueher
  gegen den Peak abgesichert werden.
- Totlaeufer werden frueher als Fehlstart beendet.
- Der grosse Katastrophenstop ist enger als vorher.


## Erwartete Wirkung Fuer Morgen

Wenn die neuen Werte ihren Zweck erfuellen, sollte morgen sichtbar sein:

- weniger spaete, "ausgereizte" Rebound-Entries
- weniger brutto-positive, netto-negative Mini-Gewinne
- weniger Ausreisser bis `-200 bps`
- mehr Trades mit klarerem Nettopuffer

Moegliche Nebenwirkung:

- Es koennen weniger Trades insgesamt entstehen.
- Das ist in Ordnung, wenn die Qualitaet pro Trade steigt.


## Morgen Pruefen

Fuer den naechsten Vergleich sollten mindestens diese Punkte erneut gemessen
werden:

- Anzahl gepaarter Trades
- Brutto positiv vs. netto positiv
- Netto-Summe des Tages
- Haeufigste Exit-Gruende
- Anzahl `hard_stop_loss`-Faelle
- Anteil kleiner Brutto-Gewinne unter `30 bps`
- wie oft Coins trotz `30` Minuten schon frueher rotiert wurden

Wenn moeglich, morgen wieder einen festen Schnitt markieren und dieselbe
Auswertung erneut darauf fahren.

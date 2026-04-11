# Exit Learning Observer

Status: 2026-04-11

## Entscheidung

Exit-Lernen wird in zwei getrennten Stufen umgesetzt.

1. Stufe 1: Beobachter aktivieren.
2. Stufe 2: spaeter adaptive Empfehlungen oder automatische Parameter-Aenderungen ableiten.

Stufe 1 darf keine Live-Exit-Schwellen selbst veraendern.

## Stufe 1

Der Core schreibt nach einem abgeschlossenen Verkauf den Journal-Event
`exit_learning_observation`.

Der Event speichert je Position unter anderem:

- Symbol und Pair
- Kaufzeit und Verkaufszeit
- Einstiegspreis und durchschnittliche Kostenbasis
- hoechster offener Gewinn seit Einstieg
- Ruecklauf vom Peak bis zum Exit
- tatsaechlicher realisierter PnL-Delta, soweit aus Core-State verfuegbar
- Exit-Grund, falls der Core vorher eine Exit-Entscheidung gesehen hat
- Alpha-Typ und aktive Strategie
- erwartete Kosten zum Entscheidungszeitpunkt

Diese Daten sind reine Messdaten. Sie beeinflussen keine Order, keinen Entry und
keinen Exit.

## Stufe 2

Spaeter kann aus den Beobachtungen eine adaptive Empfehlung entstehen, zum
Beispiel pro Coin:

- engerer Roll-Exit, wenn Gewinne oft knapp unter dem Ziel wieder abfallen
- weiterer Roll-Exit, wenn der Coin nach kleinen Ruecksetzern oft weiterlaeuft
- Mindestanzahl sauberer Beobachtungen, bevor eine Empfehlung Gewicht bekommt
- zeitlicher Verfall, damit alte Coin-Eigenschaften nicht zementiert werden

Eine automatische Live-Aenderung darf erst nach separater Freigabe erfolgen und
muss harte Grenzen behalten: kein Verkauf unter Kostenpuffer, keine Ableitung
aus einzelnen Zufallstrades.

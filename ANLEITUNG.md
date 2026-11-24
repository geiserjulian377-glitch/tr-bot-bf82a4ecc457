# Signal-Radar

Jeden Börsentag gegen 19 Uhr analysiert der Bot automatisch rund 110 Werte
weltweit: USA, Europa, Deutschland, Asien, ETFs, Krypto und Rohstoffe. Die
Ergebnisse landen in deiner App. Gleichzeitig schickt er dir eine
Push-Nachricht, die die App öffnet.

**App:** https://geiserjulian377-glitch.github.io/tr-bot-bf82a4ecc457/
Füge sie zum Home-Bildschirm hinzu: in Safari über Teilen → „Zum Home-Bildschirm“,
in Chrome über ⋮ → „Zum Startbildschirm hinzufügen“.

## Einmalige Einrichtung

1. **Repo öffentlich machen:** Settings → General → ganz unten *Danger Zone* →
   *Change visibility* → *Make public*.
2. **App einschalten:** Settings → Pages → *Source*: **GitHub Actions**.
3. **Geheimes Push-Thema eintragen:** Settings → Secrets and variables → Actions →
   *New repository secret*. Name `NTFY_TOPIC`, Wert: dein Thema (steht im Chat).
4. **In der ntfy-App** dieses Thema abonnieren. Das alte Thema kannst du löschen.
5. **Ersten Lauf starten:** Actions → *Signal-Radar* → *Run workflow*.

## Was die App zeigt

- **Lagebild:** Wie viel Prozent aller Werte gerade über ihrer 200-Tage-Linie liegen
  (die Marktbreite), dazu die Zahl der Werte pro Status.
- **Heute zu tun:** nur die neuen Kauf- und Verkaufssignale, mit Kaufkurs, Stop-Loss und Haltedauer.
- **Listen:** Signale, Halten, Kandidaten (kurz vor dem Kaufsignal) und Alle,
  filterbar nach Region, mit Suche und Sortierung.
- **Detailseite je Wert:**
  - Handlungsanweisung mit Vertrauensstufe
  - Chart (3M/6M/1J) mit SMA 20/50/200, Stop-Loss und früheren Signalen
  - Positionsrechner: wie viele Stück bei deiner Depotgröße und deinem Risiko
  - Score 0–100 mit Aufschlüsselung
  - „Warum?“ in Klartext
  - Indikatoren: RSI, MACD, 200-Tage-Abstand, Volatilität, Rückgang, 52-Wochen-Hoch
  - 5-Jahres-Backtest gegen Kaufen-und-Halten, mit Trade-Historie

## Einstellungen (`config.json`)

| Einstellung | Bedeutung |
|---|---|
| `listen` | Regionen an oder aus: `usa`, `europa`, `deutschland`, `asien`, `etfs`, `krypto`, `rohstoffe`. Die Werte stehen in `presets.json` |
| `eigene_werte` | Zusätzliche Werte, z. B. `{ "name": "Hensoldt", "ticker": "HAG.DE", "sector": "Rüstung" }`. Kürzel findest du auf finance.yahoo.com |
| `fast_sma` / `slow_sma` | Schneller und langsamer Durchschnitt. 20/50 = aktiv, 50/200 = ruhig |
| `stop_loss_pct` | Trailing-Stop in % |
| `only_send_on_signal` | `true` = Push nur bei Kauf- oder Verkaufssignal |

Jede Änderung startet automatisch einen neuen Lauf.

## Wichtig

Der Bot gibt regelbasierte Signale. Er kann die Zukunft nicht kennen. Achte auf
Score und Backtest: Bei vielen Werten war einfaches Halten in der Vergangenheit
besser als die Regel. Dies ist keine Anlageberatung.

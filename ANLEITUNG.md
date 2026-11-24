# Trading-Signal-Bot für Trade Republic

Der Bot läuft jeden Börsentag automatisch und kostenlos auf GitHub. Er
schickt dir eine Telegram-Nachricht aufs Handy: **KAUFEN**, **HALTEN** mit
Stop-Loss und erwarteter Haltedauer, **VERKAUFEN** oder **WARTEN**.
Die Order gibst du selbst in der Trade-Republic-App ein.

Die Einrichtung dauert einmalig etwa 15 Minuten. Am PC geht es leichter,
auf dem Handy im Browser ist es aber auch möglich.

---

## Schnellweg: ntfy statt Telegram (2 Min.)

1. Installiere die kostenlose App **ntfy** (App Store / Google Play). Ein Konto brauchst du nicht.
2. Tippe in der App auf **+** und abonniere das Thema aus dem Secret `NTFY_TOPIC`, z. B. `tr-bot-bf82a4ecc457`.
3. Lege auf GitHub das Secret `NTFY_TOPIC` mit genau diesem Namen an (Schritt 2.4). Die Telegram-Secrets brauchst du dann nicht.

Halte den Themennamen geheim: Wer ihn kennt, kann deine Nachrichten mitlesen.

## 1. Telegram-Bot anlegen (5 Min., alternativ zu ntfy)

1. Installiere **Telegram** auf deinem Handy.
2. Suche den Kontakt **@BotFather** (blauer Haken) und schreibe `/newbot`.
3. Gib einen Namen ein, z. B. `Mein Trading Bot`, und danach einen Benutzernamen,
   der auf `bot` endet, z. B. `max_trading_signal_bot`.
4. BotFather schickt dir einen **Token** wie `7123456789:AAH...`. **Kopiere ihn
   und gib ihn niemandem.**
5. Öffne deinen neuen Bot (Link in der Nachricht) und schreibe ihm **irgendwas**, z. B. „Hallo“.
6. Öffne im Browser:
   `https://api.telegram.org/bot<DEIN_TOKEN>/getUpdates`
   Dort steht `"chat":{"id":123456789` – diese Zahl ist deine **Chat-ID**.

## 2. GitHub einrichten (10 Min.)

1. Lege ein kostenloses Konto auf **github.com** an.
2. Erstelle ein neues Repository (**New repository**), z. B. `trading-bot`,
   und stelle es auf **Private**.
3. Lade alle Dateien aus diesem Ordner hoch (**Add file → Upload files**):
   `bot.py`, `config.json`, `ANLEITUNG.md` **und** den Ordner `.github/workflows/`
   mit `signal-bot.yml`.
   *Tipp:* Falls der Upload den versteckten Ordner `.github` nicht mitnimmt:
   **Add file → Create new file**, als Namen `.github/workflows/signal-bot.yml`
   eintippen und den Inhalt der Datei hineinkopieren.
4. Gehe zu **Settings → Secrets and variables → Actions → New repository secret** und lege zwei Secrets an:
   - `TELEGRAM_TOKEN` = dein Token aus Schritt 1.4
   - `TELEGRAM_CHAT_ID` = deine Chat-ID aus Schritt 1.6
5. Gehe auf **Actions → Trading-Signal-Bot → Run workflow**. Nach etwa
   1 Minute kommt die erste Nachricht in Telegram. ✅

Ab jetzt meldet sich der Bot **Mo–Fr um ca. 19 Uhr** (Sommerzeit 20 Uhr).

---

## Watchlist und Einstellungen ändern

Öffne `config.json` auf GitHub und tippe aufs Stift-Symbol:

| Einstellung | Bedeutung |
|---|---|
| `watchlist` | Deine Werte. `ticker` = Yahoo-Finance-Kürzel |
| `fast_sma` / `slow_sma` | Schneller/langsamer Durchschnitt in Tagen (20/50 = aktiv, 50/200 = ruhig, wenige Signale) |
| `stop_loss_pct` | Trailing-Stop in %: der Stop zieht mit steigendem Kurs nach oben |
| `position_size_eur` | Betrag pro Kauf (für die Gebührenrechnung im Backtest) |
| `fee_per_order_eur` | Trade-Republic-Gebühr pro Order (1 €) |
| `only_send_on_signal` | `true` = nur bei KAUFEN/VERKAUFEN eine Nachricht, sonst täglicher Bericht |

**Ticker finden:** Suche den Wert auf finance.yahoo.com. Für deutsche Börsenplätze
endet das Kürzel auf `.DE` (Xetra) bzw. `.F` (Frankfurt), z. B. `SAP.DE`, `ALV.DE`
(Allianz), `EUNL.DE` (iShares MSCI World), `APC.DE` (Apple in Euro). Krypto: `BTC-EUR`, `ETH-EUR`.

---

## So liest du die Nachricht

- 🟢 **JETZT KAUFEN**: Der 20-Tage-Schnitt hat den 50-Tage-Schnitt nach oben gekreuzt.
  Kaufe in Trade Republic und setze dort eine **Stop-Loss-Order** zum genannten Preis.
- 🟡 **HALTEN**: Du bist investiert. Der Stop-Loss steigt mit. Passe deine Stop-Order in TR an.
  Die Haltedauer ist ein Durchschnitt aus der Vergangenheit, **keine Vorhersage**.
- 🔴 **JETZT VERKAUFEN**: Der Trend ist gekippt oder der Stop-Loss wurde erreicht.
- ⚪ **WARTEN**: Kein Signal. Der „Abstand“ zeigt, wie nah das nächste Kaufsignal ist.
- **Backtest**: So hätte die Regel in den letzten Jahren abgeschnitten, verglichen mit Kaufen und Halten.
  Ist die Strategie bei einem Wert schlechter als Kaufen und Halten, ist dieser Wert für die Regel ungeeignet.

## Testen ohne Internet

```
python bot.py --demo    # erfundene Kurse
python bot.py --print   # echte Kurse, nur Konsole, ohne Telegram
```

## Wichtig

Der Bot gibt regelbasierte Signale. **Er kann die Zukunft nicht kennen.**
Durchschnitts-Strategien verlieren in Seitwärtsphasen oft mehrmals hintereinander
und verpassen in starken Märkten einen Teil der Rendite. Setze nur Geld ein,
dessen Verlust du verkraften kannst. Dies ist keine Anlageberatung.

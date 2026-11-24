#!/usr/bin/env python3
"""
Trade-Republic-Signal-Bot
=========================
Strategie: Kreuzung gleitender Durchschnitte (SMA) + Trailing-Stop-Loss.
- KAUFEN, wenn der schnelle Durchschnitt den langsamen von unten kreuzt.
- VERKAUFEN, wenn er ihn von oben kreuzt ODER der Stop-Loss erreicht wird.

Der Bot holt Tageskurse von Yahoo Finance, berechnet die Signale und
schickt dir eine Nachricht per Telegram. Die Order gibst du selbst in
der Trade-Republic-App ein.

Nur Python-Standardbibliothek, keine Installation nötig.

Aufruf:
  python bot.py            -> Signale berechnen und per Telegram senden
  python bot.py --print    -> nur in der Konsole ausgeben (kein Telegram)
  python bot.py --demo     -> Test mit erfundenen Kursen (ohne Internet)
"""
import json
import math
import os
import random
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- Konfiguration
def load_config():
    with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- Kursdaten
def fetch_prices(ticker, years=5):
    """Tages-Schlusskurse von Yahoo Finance: Liste von (datum, kurs)."""
    import time
    data, last_err = None, None
    for attempt, host in enumerate(["query1", "query2", "query1"]):
        url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/"
               f"{urllib.parse.quote(ticker)}?range={years}y&interval=1d")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
            break
        except Exception as e:  # z. B. kurzzeitige Sperre (429) – kurz warten, neu versuchen
            last_err = e
            time.sleep(3 * (attempt + 1))
    if data is None:
        raise RuntimeError(f"Kurse nicht abrufbar ({last_err})")
    res = data["chart"]["result"][0]
    currency = res["meta"].get("currency", "")
    closes = res["indicators"]["quote"][0]["close"]
    out = []
    for ts, c in zip(res["timestamp"], closes):
        if c is not None:
            d = datetime.fromtimestamp(ts, tz=timezone.utc).date()
            out.append((d, float(c)))
    return out, currency


def demo_prices(seed, days=1250):
    """Erfundene Kurse (Random Walk mit Trendphasen) zum Testen."""
    rnd = random.Random(seed)
    price, drift = 100.0, 0.0
    d = datetime(2021, 11, 1).date()
    out = []
    while len(out) < days:
        d += timedelta(days=1)
        if d.weekday() >= 5:
            continue
        if rnd.random() < 0.01:
            drift = rnd.uniform(-0.002, 0.003)
        price *= math.exp(drift + rnd.gauss(0, 0.015))
        out.append((d, price))
    return out, "EUR"


# ---------------------------------------------------------------- Strategie
def sma(values, n):
    out, s = [None] * len(values), 0.0
    for i, v in enumerate(values):
        s += v
        if i >= n:
            s -= values[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def simulate(prices, cfg):
    """Spielt die Strategie über die ganze Historie durch.
    Gibt abgeschlossene Trades, Ereignisse und den aktuellen Zustand zurück."""
    fast_n, slow_n = cfg["fast_sma"], cfg["slow_sma"]
    stop_pct = cfg["stop_loss_pct"] / 100
    fee, size = cfg["fee_per_order_eur"], cfg["position_size_eur"]
    dates = [d for d, _ in prices]
    closes = [c for _, c in prices]
    fast, slow = sma(closes, fast_n), sma(closes, slow_n)

    trades, events = [], []
    pos = None  # dict(entry_date, entry_price, stop)
    for i in range(slow_n, len(closes)):
        c, d = closes[i], dates[i]
        cross_up = fast[i - 1] <= slow[i - 1] and fast[i] > slow[i]
        cross_dn = fast[i - 1] >= slow[i - 1] and fast[i] < slow[i]
        if pos is None:
            if cross_up:
                pos = {"entry_date": d, "entry_price": c, "stop": c * (1 - stop_pct)}
                events.append({"date": d, "type": "BUY", "price": c})
        else:
            pos["stop"] = max(pos["stop"], c * (1 - stop_pct))  # Trailing-Stop
            reason = "Stop-Loss" if c <= pos["stop"] else ("Trendwende" if cross_dn else None)
            if reason:
                gross = c / pos["entry_price"] - 1
                net = gross - 2 * fee / size
                trades.append({
                    "entry_date": pos["entry_date"], "exit_date": d,
                    "entry": pos["entry_price"], "exit": c,
                    "days": (d - pos["entry_date"]).days,
                    "pct": net * 100, "reason": reason,
                })
                events.append({"date": d, "type": "SELL", "price": c, "reason": reason})
                pos = None

    state = {
        "date": dates[-1], "price": closes[-1],
        "fast": fast[-1], "slow": slow[-1], "position": pos,
        "trend_up": fast[-1] > slow[-1],
    }
    first = closes[slow_n]
    buy_hold = (closes[-1] / first - 1) * 100
    equity = 1.0
    for t in trades:
        equity *= 1 + t["pct"] / 100
    if pos:
        equity *= closes[-1] / pos["entry_price"]
    stats = {
        "n": len(trades),
        "win_rate": 100 * sum(t["pct"] > 0 for t in trades) / len(trades) if trades else 0,
        "avg_pct": sum(t["pct"] for t in trades) / len(trades) if trades else 0,
        "avg_days": sum(t["days"] for t in trades) / len(trades) if trades else 0,
        "min_days": min((t["days"] for t in trades), default=0),
        "max_days": max((t["days"] for t in trades), default=0),
        "strategy_pct": (equity - 1) * 100,
        "buy_hold_pct": buy_hold,
        "since": dates[slow_n],
    }
    return trades, events, state, stats


# ---------------------------------------------------------------- Nachricht
def eur(x, cur="EUR"):
    sym = {"EUR": "€", "USD": "$"}.get(cur, cur)
    s = f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{s} {sym}"


def p(x, sign=True):
    return (f"{x:+.1f}" if sign else f"{x:.0f}").replace(".", ",")


def build_message(name, ticker, cur, events, state, stats, cfg):
    today = state["date"]
    lookback = cfg.get("alert_lookback_days", 3)
    recent = [e for e in events if (today - e["date"]).days <= lookback]
    last = recent[-1] if recent else None
    pos = state["position"]
    price = eur(state["price"], cur)

    lines = [f"<b>{name}</b> ({ticker}) – {price}  <i>[{today:%d.%m.%Y}]</i>"]
    if last and last["type"] == "BUY":
        lines.append(f"🟢 <b>JETZT KAUFEN</b> (Signal vom {last['date']:%d.%m.})")
    elif last and last["type"] == "SELL":
        lines.append(f"🔴 <b>JETZT VERKAUFEN</b> – {last['reason']} (Signal vom {last['date']:%d.%m.})")
    elif pos:
        gain = (state["price"] / pos["entry_price"] - 1) * 100
        lines.append(f"🟡 HALTEN – gekauft am {pos['entry_date']:%d.%m.%Y} zu "
                     f"{eur(pos['entry_price'], cur)} ({p(gain)} %)")
    else:
        lines.append("⚪ WARTEN – kein Kaufsignal, nicht investiert sein")

    if pos:
        held = (today - pos["entry_date"]).days
        lines.append(f"Stop-Loss: <b>{eur(pos['stop'], cur)}</b> (fällt der Kurs darunter → verkaufen)")
        if stats["n"]:
            rest = max(0, round(stats["avg_days"]) - held)
            lines.append(f"Haltedauer: bis zum Verkaufssignal. Bisher {held} Tage, historisch "
                         f"Ø {stats['avg_days']:.0f} Tage ({stats['min_days']}–{stats['max_days']}), "
                         f"also grob noch ~{rest} Tage")
    else:
        gap = (state["fast"] / state["slow"] - 1) * 100
        lines.append(f"Abstand bis Kaufsignal: SMA{cfg['fast_sma']} liegt {p(gap)} % zum SMA{cfg['slow_sma']}")

    if stats["n"]:
        lines.append(f"Backtest seit {stats['since']:%Y}: {stats['n']} Trades, "
                     f"{stats['win_rate']:.0f} % Gewinner, Ø {p(stats['avg_pct'])} % je Trade | "
                     f"Strategie {stats['strategy_pct']:+.0f} % vs. Kaufen&amp;Halten {stats['buy_hold_pct']:+.0f} %")
    return "\n".join(lines), bool(last)


# ---------------------------------------------------------------- Versand
def plain(text):
    import re
    return re.sub(r"</?[bi]>", "", text).replace("&amp;", "&")


def send_ntfy(topic, title, text, urgent):
    """Push aufs Handy über die kostenlose App ntfy (kein Konto nötig)."""
    req = urllib.request.Request(
        f"https://ntfy.sh/{urllib.parse.quote(topic)}",
        data=plain(text).encode("utf-8"),
        headers={
            "Title": title.encode("utf-8"),
            "Priority": "high" if urgent else "default",
            "Tags": "rotating_light" if urgent else "chart_with_upwards_trend",
        },
    )
    urllib.request.urlopen(req, timeout=30).close()


def send(text, title, urgent, cfg):
    topic = os.environ.get("NTFY_TOPIC") or cfg.get("ntfy_topic")
    if topic:
        send_ntfy(topic, title, text, urgent)
    else:
        send_telegram(text)


def send_telegram(text):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise SystemExit("Weder NTFY_TOPIC noch TELEGRAM_TOKEN/TELEGRAM_CHAT_ID gesetzt (siehe ANLEITUNG.md).")
    body = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body)
    with urllib.request.urlopen(req, timeout=30) as r:
        if json.load(r).get("ok") is not True:
            raise RuntimeError("Telegram hat die Nachricht abgelehnt")


# ---------------------------------------------------------------- Hauptprogramm
def main():
    args = set(sys.argv[1:])
    demo, only_print = "--demo" in args, "--print" in args or "--demo" in args
    cfg = load_config()
    blocks, any_signal, errors = [], False, []

    for i, item in enumerate(cfg["watchlist"]):
        name, ticker = item["name"], item["ticker"]
        try:
            prices, cur = demo_prices(seed=i) if demo else fetch_prices(ticker, cfg.get("history_years", 5))
            if len(prices) < cfg["slow_sma"] + 2:
                raise ValueError("zu wenig Kursdaten")
            _, events, state, stats = simulate(prices, cfg)
            text, signal = build_message(name, ticker, cur, events, state, stats, cfg)
            any_signal |= signal
            blocks.append(("‼️ " if signal else "") + text)
        except Exception as e:  # ein Wert darf nicht den ganzen Bericht stoppen
            errors.append(f"⚠️ {name} ({ticker}): {e}")

    header = "🚨 <b>NEUES SIGNAL</b>\n\n" if any_signal else "📊 <b>Tagesbericht</b>\n\n"
    msg = header + "\n\n".join(blocks + errors)
    msg += "\n\n<i>Keine Anlageberatung – regelbasierte Signale, Verluste möglich.</i>"

    if cfg.get("only_send_on_signal") and not any_signal and not only_print:
        print("Kein neues Signal – keine Nachricht gesendet.")
        return
    if only_print:
        print(plain(msg))
    else:
        send(msg, "Trading-Bot: NEUES SIGNAL" if any_signal else "Trading-Bot: Tagesbericht", any_signal, cfg)
        print("Nachricht gesendet.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Trade-Republic-Signal-Bot
=========================
Strategie: Kreuzung gleitender Durchschnitte (SMA) + Trailing-Stop-Loss.
- KAUFEN, wenn der schnelle Durchschnitt den langsamen von unten kreuzt.
- VERKAUFEN, wenn er ihn von oben kreuzt ODER der Stop-Loss erreicht wird.

Holt Tageskurse von Yahoo Finance, zeichnet ein Dashboard und schickt es
als Push-Nachricht (App "ntfy") aufs Handy. Die Order gibst du selbst in
der Trade-Republic-App ein.

  python bot.py            -> berechnen und senden
  python bot.py --print    -> nur Konsole + Bilder lokal, nichts senden
  python bot.py --demo     -> wie --print, mit erfundenen Kursen
"""
import json
import math
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- Konfiguration
def load_json(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return json.load(f)


def watchlist(cfg):
    presets = load_json("presets.json")
    items, seen = [], set()
    for key, on in cfg.get("listen", {}).items():
        if on:
            items += [{"name": n, "ticker": t} for n, t in presets.get(key, [])]
    items += cfg.get("eigene_werte", []) + cfg.get("watchlist", [])
    out = []
    for it in items:
        if it["ticker"] not in seen:
            seen.add(it["ticker"])
            out.append(it)
    return out


# ---------------------------------------------------------------- Kursdaten
def fetch_prices(ticker, years=5):
    data, last_err = None, None
    for attempt, host in enumerate(["query1", "query2", "query1"]):
        url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/"
               f"{urllib.parse.quote(ticker)}?range={years}y&interval=1d")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
            break
        except Exception as e:
            last_err = e
            time.sleep(3 * (attempt + 1))
    if data is None:
        raise RuntimeError(f"Kurse nicht abrufbar ({last_err})")
    res = data["chart"]["result"][0]
    closes = res["indicators"]["quote"][0]["close"]
    out = [(datetime.fromtimestamp(ts, tz=timezone.utc).date(), float(c))
           for ts, c in zip(res["timestamp"], closes) if c is not None]
    return out, res["meta"].get("currency", "")


def demo_prices(seed, days=1250):
    rnd = random.Random(seed)
    price, drift = rnd.uniform(20, 400), 0.0
    d, out = datetime(2021, 11, 1).date(), []
    while len(out) < days:
        d += timedelta(days=1)
        if d.weekday() >= 5:
            continue
        if rnd.random() < 0.012:
            drift = rnd.uniform(-0.003, 0.004)
        price *= math.exp(drift + rnd.gauss(0, 0.016))
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
    fast_n, slow_n = cfg["fast_sma"], cfg["slow_sma"]
    stop_pct = cfg["stop_loss_pct"] / 100
    fee, size = cfg["fee_per_order_eur"], cfg["position_size_eur"]
    dates = [d for d, _ in prices]
    closes = [c for _, c in prices]
    fast, slow = sma(closes, fast_n), sma(closes, slow_n)
    trades, events, pos = [], [], None
    for i in range(slow_n, len(closes)):
        c, d = closes[i], dates[i]
        up = fast[i - 1] <= slow[i - 1] and fast[i] > slow[i]
        dn = fast[i - 1] >= slow[i - 1] and fast[i] < slow[i]
        if pos is None:
            if up:
                pos = {"entry_date": d, "entry_price": c, "stop": c * (1 - stop_pct)}
                events.append({"i": i, "date": d, "type": "BUY", "price": c})
        else:
            pos["stop"] = max(pos["stop"], c * (1 - stop_pct))
            reason = "Stop-Loss" if c <= pos["stop"] else ("Trendwende" if dn else None)
            if reason:
                net = c / pos["entry_price"] - 1 - 2 * fee / size
                trades.append({"days": (d - pos["entry_date"]).days, "pct": net * 100})
                events.append({"i": i, "date": d, "type": "SELL", "price": c, "reason": reason})
                pos = None
    equity = 1.0
    for t in trades:
        equity *= 1 + t["pct"] / 100
    if pos:
        equity *= closes[-1] / pos["entry_price"]
    n = len(trades)
    stats = {
        "n": n,
        "win_rate": 100 * sum(t["pct"] > 0 for t in trades) / n if n else 0,
        "avg_pct": sum(t["pct"] for t in trades) / n if n else 0,
        "avg_days": sum(t["days"] for t in trades) / n if n else 0,
        "strategy_pct": (equity - 1) * 100,
        "buy_hold_pct": (closes[-1] / closes[slow_n] - 1) * 100,
        "since": dates[slow_n],
    }
    return {"dates": dates, "closes": closes, "fast": fast, "slow": slow,
            "events": events, "position": pos, "stats": stats}


def analyse(item, prices, cur, cfg):
    sim = simulate(prices, cfg)
    today = sim["dates"][-1]
    recent = [e for e in sim["events"] if (today - e["date"]).days <= cfg.get("alert_lookback_days", 3)]
    last = recent[-1] if recent else None
    pos = sim["position"]
    status = last["type"] if last else ("HOLD" if pos else "WAIT")
    return {**sim, **item, "cur": cur, "date": today, "price": sim["closes"][-1],
            "status": status, "last": last,
            "gap": (sim["fast"][-1] / sim["slow"][-1] - 1) * 100,
            "held": (today - pos["entry_date"]).days if pos else 0}


# ---------------------------------------------------------------- Texte
def money(x, cur):
    sym = {"EUR": "€", "USD": "$"}.get(cur, cur)
    return f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") + f" {sym}"


def pct(x):
    return f"{x:+.1f} %".replace(".", ",")


def signal_text(r):
    st, pos, last = r["stats"], r["position"], r["last"]
    lines = []
    if r["status"] == "BUY":
        lines.append(f"Kaufen zu ca. {money(r['price'], r['cur'])}")
        lines.append(f"Stop-Loss setzen: {money(pos['stop'], r['cur'])}")
        lines.append(f"Halten bis zum Verkaufssignal, historisch Ø {st['avg_days']:.0f} Tage")
    else:
        lines.append(f"Verkaufen zu ca. {money(r['price'], r['cur'])} – Grund: {last['reason']}")
    lines.append(f"Backtest: {st['n']} Trades, {st['win_rate']:.0f} % Treffer, Ø {pct(st['avg_pct'])}")
    return "\n".join(lines)


def summary_text(results, failed):
    def names(s, extra=None, limit=8):
        rs = [r for r in results if r["status"] == s]
        txt = ", ".join(f"{r['name']}" + (f" ({extra(r)})" if extra else "") for r in rs[:limit]) or "–"
        return txt + (f" und {len(rs) - limit} weitere" if len(rs) > limit else "")
    hold = lambda r: pct((r["price"] / r["position"]["entry_price"] - 1) * 100)
    near = sorted((r for r in results if r["status"] == "WAIT" and r["gap"] < 0),
                  key=lambda r: -r["gap"])[:4]
    lines = [f"🟢 Kaufen: {names('BUY')}", f"🔴 Verkaufen: {names('SELL')}",
             f"🟡 Halten: {names('HOLD', hold)}",
             "👀 Bald Kaufsignal?: " + (", ".join(f"{r['name']} ({pct(r['gap'])})" for r in near) or "–")]
    if failed:
        lines.append(f"⚠️ Nicht geladen: {', '.join(failed)}")
    return "\n".join(lines)


def pick_cards(results, n):
    sig = [r for r in results if r["status"] in ("BUY", "SELL")]
    hold = sorted((r for r in results if r["status"] == "HOLD"),
                  key=lambda r: -(r["price"] / r["position"]["entry_price"]))
    near = sorted((r for r in results if r["status"] == "WAIT" and r["gap"] < 0), key=lambda r: -r["gap"])
    return (sig + hold + near[:3])[:n] or results[:n]


# ---------------------------------------------------------------- Versand (ntfy)
def ntfy(topic, title, message, image=None, priority="default", tags="", actions=""):
    params = {"title": title, "message": message[:3900], "priority": priority, "tags": tags}
    if actions:
        params["actions"] = actions
    if image:
        params["filename"] = os.path.basename(image)
        with open(image, "rb") as f:
            body = f.read()
    else:
        body = b""
    url = f"https://ntfy.sh/{urllib.parse.quote(topic)}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, data=body, method="PUT")
    urllib.request.urlopen(req, timeout=60).close()


def links(r):
    yahoo = f"https://finance.yahoo.com/quote/{urllib.parse.quote(r['ticker'])}"
    return f"view, Chart, {yahoo}; view, Trade Republic, https://app.traderepublic.com"


# ---------------------------------------------------------------- Hauptprogramm
def main():
    args = set(sys.argv[1:])
    demo = "--demo" in args
    dry = demo or "--print" in args
    cfg = load_json("config.json")
    import render

    items = watchlist(cfg)
    results, failed = [], []
    for i, item in enumerate(items):
        try:
            if demo:
                prices, cur = demo_prices(i)
            else:
                prices, cur = fetch_prices(item["ticker"], cfg.get("history_years", 5))
                time.sleep(0.3)
            if len(prices) < cfg["slow_sma"] + 2:
                raise ValueError("zu wenig Kursdaten")
            results.append(analyse(item, prices, cur, cfg))
        except Exception as e:
            print(f"FEHLER {item['name']} ({item['ticker']}): {e}")
            failed.append(item["name"])
    if not results:
        raise SystemExit("Keine Kurse geladen.")

    date = max(r["date"] for r in results)
    out = os.path.join(HERE, "dashboard.png")
    picked = pick_cards(results, cfg.get("dashboard_max_karten", 10))
    render.dashboard(results, picked, len(items), failed, date, cfg, out)
    summary = summary_text(results, failed)
    signals = [r for r in results if r["status"] in ("BUY", "SELL")]
    print(summary)

    if dry:
        for r in signals[:3]:
            render.signal_chart(r, cfg, os.path.join(HERE, f"signal_{r['ticker']}.png"))
            print(f"\n[{r['status']}] {r['name']}\n{signal_text(r)}")
        return
    if cfg.get("only_send_on_signal") and not signals:
        print("Kein neues Signal – nichts gesendet.")
        return

    topic = os.environ.get("NTFY_TOPIC") or cfg["ntfy_topic"]
    n_buy = sum(r["status"] == "BUY" for r in signals)
    title = (f"Signal-Radar: {n_buy}× Kaufen, {len(signals) - n_buy}× Verkaufen"
             if signals else f"Signal-Radar {date:%d.%m.} – keine neuen Signale")
    ntfy(topic, title, summary, image=out, priority="high" if signals else "default",
         tags="rotating_light" if signals else "bar_chart")

    if cfg.get("einzel_push_pro_signal", True):
        for r in signals[:8]:
            img = os.path.join(HERE, "signal.png")
            render.signal_chart(r, cfg, img)
            buy = r["status"] == "BUY"
            ntfy(topic, f"{'KAUFEN' if buy else 'VERKAUFEN'}: {r['name']}", signal_text(r), image=img,
                 priority="urgent" if buy else "high", tags="green_circle" if buy else "red_circle",
                 actions=links(r))
            time.sleep(1)
    print("Gesendet.")


if __name__ == "__main__":
    main()

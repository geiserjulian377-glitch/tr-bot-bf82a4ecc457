#!/usr/bin/env python3
"""
Signal-Radar – Analyse-Bot für Trade Republic
=============================================
Holt jeden Börsentag Kurse von Yahoo Finance für ~110 Werte weltweit,
analysiert Trend, Momentum, RSI, MACD, Volatilität und einen 5-Jahres-
Backtest, schreibt alles nach site/data.json (die App) und schickt eine
kurze Push-Nachricht (App "ntfy"), die die App öffnet.

Handelsregel: KAUFEN, wenn SMA20 den SMA50 von unten kreuzt; VERKAUFEN bei
Kreuzung nach unten oder wenn der Trailing-Stop-Loss erreicht wird.

  python bot.py           -> berechnen, App-Daten schreiben, Push senden
  python bot.py --print   -> berechnen, nichts senden
  python bot.py --demo    -> erfundene Kurse, nichts senden (Test ohne Internet)
  python bot.py --no-push -> berechnen, Push für später in push.json ablegen
  python bot.py --notify  -> Push aus push.json senden
"""
import json
import math
import os
import random
import re
import statistics
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REGION = {"usa": "USA", "europa": "Europa", "deutschland": "Deutschland", "asien": "Asien",
          "etfs": "ETFs", "krypto": "Krypto", "rohstoffe": "Rohstoffe"}


def load_json(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return json.load(f)


def watchlist(cfg):
    presets, out, seen = load_json("presets.json"), [], set()
    for key, on in cfg.get("listen", {}).items():
        if on:
            for name, ticker, sector in presets.get(key, []):
                out.append({"name": name, "ticker": ticker, "sector": sector, "region": REGION.get(key, key)})
    for it in cfg.get("eigene_werte", []):
        out.append({"sector": "Eigene", "region": "Eigene", **it})
    uniq = []
    for it in out:
        if it["ticker"] not in seen:
            seen.add(it["ticker"])
            uniq.append(it)
    return uniq


# ---------------------------------------------------------------- Kursdaten
def fetch_prices(ticker, years=5):
    data, err = None, None
    for attempt, host in enumerate(["query1", "query2", "query1"]):
        url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/"
               f"{urllib.parse.quote(ticker)}?range={years}y&interval=1d")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
            break
        except Exception as e:
            err = e
            time.sleep(2 * (attempt + 1))
    if data is None:
        raise RuntimeError(f"Kurse nicht abrufbar ({err})")
    res = data["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    rows = []
    for i, ts in enumerate(res["timestamp"]):
        c = q["close"][i]
        if c is None:
            continue
        g = lambda k: (q.get(k) or [None] * (i + 1))[i]
        o, h, l, v = g("open"), g("high"), g("low"), g("volume")
        rows.append((datetime.fromtimestamp(ts, tz=timezone.utc).date(), float(c),
                     float(o if o is not None else c), float(h if h is not None else c),
                     float(l if l is not None else c), int(v or 0)))
    return rows, res["meta"].get("currency") or "EUR"


# ---------------------------------------------------------------- Analysten (Yahoo quoteSummary)
_YAHOO = {"opener": None, "crumb": None, "tried": False}


def _yahoo_session():
    if _YAHOO["tried"]:
        return _YAHOO["opener"], _YAHOO["crumb"]
    _YAHOO["tried"] = True
    import http.cookiejar
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    op.addheaders = [("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")]
    for url in ("https://fc.yahoo.com", "https://finance.yahoo.com/quote/AAPL"):
        try:
            op.open(url, timeout=20).read(2000)
        except Exception:
            pass
    try:
        crumb = op.open("https://query2.finance.yahoo.com/v1/test/getcrumb", timeout=20).read().decode().strip()
        if crumb and "<" not in crumb and len(crumb) < 40:
            _YAHOO.update(opener=op, crumb=crumb)
    except Exception as e:
        print(f"Analysten-Daten nicht verfügbar (Crumb): {e}")
    return _YAHOO["opener"], _YAHOO["crumb"]


def fetch_analyst(ticker):
    op, crumb = _yahoo_session()
    if not crumb:
        return None
    url = (f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{urllib.parse.quote(ticker)}"
           f"?modules=financialData,recommendationTrend&crumb={urllib.parse.quote(crumb)}")
    try:
        res = json.load(op.open(url, timeout=20))["quoteSummary"]["result"][0]
    except Exception:
        return None
    fd = res.get("financialData", {})
    raw = lambda k: (fd.get(k) or {}).get("raw") if isinstance(fd.get(k), dict) else None
    n = raw("numberOfAnalystOpinions")
    mean = raw("targetMeanPrice")
    if not n or not mean:
        return None
    trend = (res.get("recommendationTrend", {}).get("trend") or [{}])[0]
    return {
        "n": int(n), "mean": mean, "high": raw("targetHighPrice"), "low": raw("targetLowPrice"),
        "rec": fd.get("recommendationKey"), "rec_mean": raw("recommendationMean"),
        "cur": fd.get("financialCurrency"),
        "votes": {k: int(trend.get(k, 0) or 0) for k in ("strongBuy", "buy", "hold", "sell", "strongSell")},
    }


def demo_analyst(seed, price):
    rnd = random.Random(seed * 7)
    if rnd.random() < 0.15:
        return None
    m = price * rnd.uniform(0.85, 1.6)
    v = {"strongBuy": rnd.randint(0, 12), "buy": rnd.randint(0, 15), "hold": rnd.randint(0, 10),
         "sell": rnd.randint(0, 3), "strongSell": rnd.randint(0, 1)}
    return {"n": sum(v.values()) or 1, "mean": m, "high": m * 1.4, "low": m * 0.7, "rec": "buy",
            "rec_mean": rnd.uniform(1.4, 3.2), "cur": None, "votes": v}


def demo_prices(seed, days=1260):
    rnd = random.Random(seed)
    price, drift, d, out = rnd.uniform(20, 400), 0.0, datetime(2021, 11, 1).date(), []
    while len(out) < days:
        d += timedelta(days=1)
        if d.weekday() >= 5:
            continue
        if rnd.random() < 0.012:
            drift = rnd.uniform(-0.003, 0.004)
        prev = price
        price *= math.exp(drift + rnd.gauss(0, 0.017))
        hi = max(prev, price) * (1 + abs(rnd.gauss(0, 0.006)))
        lo = min(prev, price) * (1 - abs(rnd.gauss(0, 0.006)))
        out.append((d, price, prev, hi, lo, int(rnd.uniform(5e5, 3e6))))
    return out, rnd.choice(["EUR", "EUR", "USD", "CHF", "GBp"])


def fx_rates(currencies, demo):
    """Wie viele Einheiten der Währung ein Euro kostet."""
    demo_fx = {"USD": 1.08, "CHF": 0.94, "GBP": 0.85, "DKK": 7.46, "HKD": 8.4, "JPY": 162.0, "KRW": 1480.0}
    rates = {"EUR": 1.0}
    for cur in currencies:
        base = "GBP" if cur == "GBp" else cur
        if base in rates or cur == "EUR":
            continue
        try:
            rates[base] = demo_fx.get(base, 1.0) if demo else fetch_prices(f"EUR{base}=X", 1)[0][-1][1]
        except Exception as e:
            print(f"FX {base}: {e}")
    if "GBP" in rates:
        rates["GBp"] = rates["GBP"] * 100
    return rates


# ---------------------------------------------------------------- Indikatoren
def sma(v, n):
    out, s = [None] * len(v), 0.0
    for i, x in enumerate(v):
        s += x
        if i >= n:
            s -= v[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(v, n):
    k, out, e = 2 / (n + 1), [], v[0]
    for x in v:
        e = x * k + e * (1 - k)
        out.append(e)
    return out


def rsi(v, n=14):
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = v[i] - v[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(v)):
        d = v[i] - v[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def rsi_series(v, n=14):
    out = [None] * len(v)
    if len(v) <= n:
        return out
    ag = sum(max(v[i] - v[i - 1], 0) for i in range(1, n + 1)) / n
    al = sum(max(v[i - 1] - v[i], 0) for i in range(1, n + 1)) / n
    for i in range(n + 1, len(v)):
        d = v[i] - v[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def swing_levels(highs, lows, price, look=130, w=7):
    """Nächste Unterstützung unter und nächster Widerstand über dem Kurs (lokale Tiefs/Hochs)."""
    hs, ls = highs[-look:], lows[-look:]
    sup = [ls[i] for i in range(w, len(ls) - w) if ls[i] == min(ls[i - w:i + w + 1]) and ls[i] < price]
    res = [hs[i] for i in range(w, len(hs) - w) if hs[i] == max(hs[i - w:i + w + 1]) and hs[i] > price]
    return (max(sup) if sup else min(ls)), (min(res) if res else None)


def ret(v, days):
    return (v[-1] / v[-1 - days] - 1) * 100 if len(v) > days else None


def max_drawdown(v):
    peak, dd = v[0], 0.0
    for x in v:
        peak = max(peak, x)
        dd = min(dd, x / peak - 1)
    return dd * 100


# ---------------------------------------------------------------- Strategie + Backtest
def simulate(dates, closes, fast, slow, cfg):
    stop_pct = cfg["stop_loss_pct"] / 100
    fee_frac = 2 * cfg["fee_per_order_eur"] / cfg["position_size_eur"]
    start = cfg["slow_sma"]
    trades, events, pos, equity, curve = [], [], None, 1.0, []
    for i in range(start, len(closes)):
        c, d = closes[i], dates[i]
        up = fast[i - 1] <= slow[i - 1] and fast[i] > slow[i]
        dn = fast[i - 1] >= slow[i - 1] and fast[i] < slow[i]
        if pos is None and up:
            pos = {"entry_date": d, "entry_price": c, "stop": c * (1 - stop_pct), "i": i}
            events.append({"i": i, "date": d, "type": "BUY", "price": c})
        elif pos is not None:
            pos["stop"] = max(pos["stop"], c * (1 - stop_pct))
            reason = "Stop-Loss" if c <= pos["stop"] else ("Trendwende" if dn else None)
            if reason:
                pct = (c / pos["entry_price"] - 1 - fee_frac) * 100
                equity *= 1 + pct / 100
                trades.append({"in": str(pos["entry_date"]), "out": str(d), "entry": pos["entry_price"], "exit": c,
                               "days": (d - pos["entry_date"]).days, "pct": pct, "reason": reason})
                events.append({"i": i, "date": d, "type": "SELL", "price": c, "reason": reason})
                pos = None
        curve.append(equity * (c / pos["entry_price"] if pos else 1))
    years = max((dates[-1] - dates[start]).days / 365.25, 0.1)
    final = curve[-1] if curve else 1
    wins = [t["pct"] for t in trades if t["pct"] > 0]
    losses = [t["pct"] for t in trades if t["pct"] <= 0]
    n = len(trades)
    bh = closes[-1] / closes[start]
    stats = {
        "n": n,
        "win_rate": 100 * len(wins) / n if n else 0,
        "avg_pct": sum(t["pct"] for t in trades) / n if n else 0,
        "avg_win": sum(wins) / len(wins) if wins else 0,
        "avg_loss": sum(losses) / len(losses) if losses else 0,
        "profit_factor": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None,
        "avg_days": sum(t["days"] for t in trades) / n if n else 0,
        "min_days": min((t["days"] for t in trades), default=0),
        "max_days": max((t["days"] for t in trades), default=0),
        "strategy_pct": (final - 1) * 100,
        "buy_hold_pct": (bh - 1) * 100,
        "strategy_cagr": (final ** (1 / years) - 1) * 100,
        "buy_hold_cagr": (bh ** (1 / years) - 1) * 100,
        "strategy_maxdd": max_drawdown(curve) if curve else 0,
        "buy_hold_maxdd": max_drawdown(closes[start:]),
        "years": years,
    }
    return trades, events, pos, stats


# ---------------------------------------------------------------- Analyse eines Werts
def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def analyse(item, prices, cur, fx, cfg, analyst=None):
    dates = [p[0] for p in prices]
    closes = [p[1] for p in prices]
    fast, slow = sma(closes, cfg["fast_sma"]), sma(closes, cfg["slow_sma"])
    s200 = sma(closes, 200)
    trades, events, pos, st = simulate(dates, closes, fast, slow, cfg)
    today, price = dates[-1], closes[-1]

    lookback = cfg.get("alert_lookback_days", 3)
    recent = [e for e in events if (today - e["date"]).days <= lookback]
    last = recent[-1] if recent else None
    status = last["type"] if last else ("HOLD" if pos else "WAIT")

    macd_line = [a - b for a, b in zip(ema(closes, 12), ema(closes, 26))]
    macd_sig = ema(macd_line, 9)
    macd_hist = macd_line[-1] - macd_sig[-1]
    logrets = [math.log(closes[i] / closes[i - 1]) for i in range(max(1, len(closes) - 63), len(closes))]
    vol = statistics.pstdev(logrets) * math.sqrt(252) * 100 if len(logrets) > 2 else 0
    year = closes[-252:]
    ind = {
        "rsi": rsi(closes[-300:]),
        "macd_hist_pct": macd_hist / price * 100,
        "dist200": (price / s200[-1] - 1) * 100 if s200[-1] else None,
        "slope50": (slow[-1] / slow[-21] - 1) * 100 if len(slow) > 21 and slow[-21] else None,
        "gap": (fast[-1] / slow[-1] - 1) * 100,
        "r1d": ret(closes, 1), "r1m": ret(closes, 21), "r3m": ret(closes, 63), "r1y": ret(closes, 252),
        "vol": vol,
        "dd1y": max_drawdown(year),
        "from_high": (price / max(year) - 1) * 100,
    }

    # ---- Score 0–100: Trend 40, Momentum 25, RSI 15, Backtest 15, Risiko 5
    parts = {}
    parts["Trend"] = (10 if ind["dist200"] is None else 20 if ind["dist200"] > 0 else 0) \
        + (10 if (ind["slope50"] or 0) > 0 else 0) + (10 if ind["gap"] > 0 else 0)
    parts["Momentum"] = round(clamp(((ind["r3m"] or 0) + 10) / 30) * 15 + (10 if macd_hist > 0 else 0))
    r = ind["rsi"]
    parts["RSI"] = 15 if 45 <= r <= 65 else 10 if (35 <= r < 45 or 65 < r <= 70) else 4 if r > 70 else 6
    parts["Backtest"] = round(clamp((st["win_rate"] - 25) / 40) * 10
                              + (5 if st["strategy_pct"] > st["buy_hold_pct"] else 0))
    parts["Risiko"] = 5 if vol < 30 else 3 if vol < 50 else 0
    score = int(sum(parts.values()))
    maxes = {"Trend": 40, "Momentum": 25, "RSI": 15, "Backtest": 15, "Risiko": 5}

    # ---- Begründung in Klartext
    why = []

    def add(tone, text):
        why.append({"t": tone, "x": text})

    def de(x, f=".1f"):
        return format(x, f).replace(".", ",")

    if ind["dist200"] is not None:
        if ind["dist200"] > 0:
            add("+", f"Kurs liegt {de(ind['dist200'])} % über der 200-Tage-Linie – langfristiger Aufwärtstrend.")
        else:
            add("-", f"Kurs liegt {de(abs(ind['dist200']))} % unter der 200-Tage-Linie – langfristig im Abwärtstrend.")
    else:
        add("~", f"Erst {len(closes)} Handelstage an der Börse – langfristiger Trend und Statistik noch begrenzt.")
    add("+" if ind["gap"] > 0 else "-",
        f"SMA{cfg['fast_sma']} liegt {de(ind['gap'], '+.1f')} % zum SMA{cfg['slow_sma']} – "
        + ("kurzfristiger Trend zeigt nach oben." if ind["gap"] > 0 else "kurzfristiger Trend zeigt nach unten."))
    if r > 70:
        add("-", f"RSI {r:.0f}: überkauft – Rücksetzer wahrscheinlicher, nicht hinterherlaufen.")
    elif r < 30:
        add("~", f"RSI {r:.0f}: überverkauft – Gegenbewegung möglich, Trend aber noch schwach.")
    elif 45 <= r <= 65:
        add("+", f"RSI {r:.0f}: gesunder Bereich – weder überkauft noch überverkauft.")
    else:
        add("~", f"RSI {r:.0f}: neutral, " + ("eher schwach." if r < 45 else "schon recht heiß."))
    add("+" if macd_hist > 0 else "-", "MACD über der Signallinie – Momentum nimmt zu." if macd_hist > 0
        else "MACD unter der Signallinie – Momentum lässt nach.")
    if vol >= 50:
        add("-", f"Sehr schwankungsreich ({vol:.0f} % p.a.) – kleinere Position wählen.")
    if st["n"] >= 3:
        better = st["strategy_pct"] > st["buy_hold_pct"]
        add("+" if better else "-",
            f"Backtest {st['years']:.0f} Jahre: Regel {st['strategy_pct']:+.0f} % vs. Halten {st['buy_hold_pct']:+.0f} % – "
            + ("die Signale haben hier funktioniert." if better else "Halten war hier besser, Signale mit Vorsicht."))
    else:
        add("~", "Zu wenige historische Signale für eine verlässliche Statistik.")

    # ---- Analysten
    an = None
    if analyst and analyst.get("mean"):
        v = analyst["votes"]
        pos_votes = v["strongBuy"] + v["buy"]
        tot_votes = sum(v.values()) or 0
        up = (analyst["mean"] / price - 1) * 100
        rm = analyst.get("rec_mean") or 3
        label = ("Starker Kauf" if rm <= 1.6 else "Kaufen" if rm <= 2.4 else "Halten" if rm <= 3.4 else "Verkaufen")
        an = {**analyst, "upside": up, "label": label,
              "buy_share": 100 * pos_votes / tot_votes if tot_votes else None}
        if rm <= 2.4 and up > 15:
            add("+", f"Analysten: {analyst['n']} Experten sagen im Schnitt „{label}“, Kursziel "
                     f"{de(analyst['mean'], '.2f')} {cur} = {de(up, '+.0f')} % Potenzial in 12 Monaten.")
        elif up < 0:
            add("-", f"Analysten: Ø Kursziel {de(analyst['mean'], '.2f')} {cur} liegt {de(abs(up), '.0f')} % unter dem Kurs.")
        else:
            add("~", f"Analysten: „{label}“, Ø Kursziel {de(analyst['mean'], '.2f')} {cur} ({de(up, '+.0f')} %).")

    # ---- Handlungsanweisung
    rate = fx.get(cur) or 1.0
    rec = {"status": status, "confidence": "Hoch" if score >= 70 else "Mittel" if score >= 50 else "Niedrig"}
    if pos:
        rec.update({"entry": pos["entry_price"], "entry_date": str(pos["entry_date"]), "stop": pos["stop"],
                    "risk_pct": (price / pos["stop"] - 1) * 100,
                    "gain_pct": (price / pos["entry_price"] - 1) * 100,
                    "held": (today - pos["entry_date"]).days})
    if last and last["type"] == "SELL":
        rec["reason"] = last["reason"]
    if status == "BUY":
        rec["text"] = ("Kaufen und sofort eine Stop-Loss-Order setzen. Halten, bis ein Verkaufssignal kommt "
                       f"(historisch Ø {st['avg_days']:.0f} Tage).")
    elif status == "SELL":
        rec["text"] = f"Verkaufen – {last['reason']}. Danach auf das nächste Kaufsignal warten."
    elif status == "HOLD":
        rec["text"] = (f"Halten und den Stop-Loss nachziehen. Seit {rec['held']} Tagen investiert, "
                       f"historisch Ø {st['avg_days']:.0f} Tage.")
    else:
        near = -2 < ind["gap"] < 0 and (ind["slope50"] or 0) > -1
        rec["watch"] = near
        rec["text"] = ("Noch nicht kaufen – das Kaufsignal rückt näher. Beobachten." if near
                       else "Nicht kaufen. Kein Aufwärtstrend – auf ein Kaufsignal warten.")

    # ---- Chartdaten (letzte ~260 Tage täglich + bis 5 Jahre wöchentlich)
    n0 = max(0, len(closes) - 260)
    rnd = lambda x: None if x is None else float(f"{x:.5g}")
    opens = [p[2] for p in prices]
    highs = [p[3] for p in prices]
    lows = [p[4] for p in prices]
    vols = [p[5] for p in prices]
    rsis = rsi_series(closes)
    hist = [a - b for a, b in zip(macd_line, macd_sig)]
    sup, resi = swing_levels(highs, lows, price)
    wk = list(range(len(closes) - 1, -1, -5))[::-1]
    chart = {
        "d": [str(d) for d in dates[n0:]],
        "o": [rnd(x) for x in opens[n0:]], "h": [rnd(x) for x in highs[n0:]],
        "lo": [rnd(x) for x in lows[n0:]], "c": [rnd(x) for x in closes[n0:]],
        "v": vols[n0:],
        "f": [rnd(x) for x in fast[n0:]],
        "s": [rnd(x) for x in slow[n0:]],
        "l": [rnd(x) for x in s200[n0:]],
        "rsi": [None if x is None else round(x, 1) for x in rsis[n0:]],
        "macd": [rnd(x) for x in hist[n0:]],
        "e": [{"i": e["i"] - n0, "t": e["type"]} for e in events if e["i"] >= n0],
        "w": {"d": [str(dates[i]) for i in wk], "c": [rnd(closes[i]) for i in wk],
              "l": [rnd(s200[i]) for i in wk],
              "e": [{"d": str(e["date"]), "t": e["type"]} for e in events]},
        "lv": {"sup": rnd(sup), "res": rnd(resi), "hi52": rnd(max(highs[-252:])), "lo52": rnd(min(lows[-252:]))},
    }
    spark = [rnd(x) for x in closes[-90::3]]

    return {
        **item, "id": re.sub(r"[^A-Za-z0-9]+", "-", item["ticker"]).strip("-"),
        "cur": cur, "eur_rate": 1 / rate, "price": price, "date": str(today),
        "status": status, "score": score, "parts": parts, "parts_max": maxes, "analyst": an,
        "ind": {**ind, "sup_pct": (sup / price - 1) * 100, "res_pct": (resi / price - 1) * 100 if resi else None},
        "stats": st, "rec": rec, "why": why, "spark": spark,
        "_detail": {"chart": chart, "trades": trades[-10:][::-1]},
    }


# ---------------------------------------------------------------- Push (ntfy)
def ntfy(topic, title, message, click="", priority="default", tags="", actions=""):
    params = {"title": title, "message": message[:3900], "priority": priority, "tags": tags}
    if click:
        params["click"] = click
    if actions:
        params["actions"] = actions
    url = f"https://ntfy.sh/{urllib.parse.quote(topic)}?" + urllib.parse.urlencode(params)
    urllib.request.urlopen(urllib.request.Request(url, data=b"", method="PUT"), timeout=60).close()


def fmt(x, cur):
    sym = {"EUR": "€", "USD": "$"}.get(cur, cur)
    return f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") + f" {sym}"


# ---------------------------------------------------------------- Hauptprogramm
def main():
    args = set(sys.argv[1:])
    if "--notify" in args:
        return notify(os.path.join(HERE, "push.json"))
    demo = "--demo" in args
    dry = demo or "--print" in args
    cfg = load_json("config.json")
    items = watchlist(cfg)

    raw, failed = [], []
    for i, it in enumerate(items):
        try:
            prices, cur = demo_prices(i) if demo else fetch_prices(it["ticker"], cfg.get("history_years", 5))
            if not demo:
                time.sleep(0.25)
            if len(prices) < cfg["slow_sma"] + 15:
                raise ValueError(f"nur {len(prices)} Kurstage")
            raw.append((it, prices, cur))
        except Exception as e:
            print(f"FEHLER {it['name']} ({it['ticker']}): {e}")
            failed.append(it["name"])
    if not raw:
        raise SystemExit("Keine Kurse geladen.")

    fx = fx_rates({cur for _, _, cur in raw}, demo)
    results = []
    n_an = 0
    for k, (it, prices, cur) in enumerate(raw):
        try:
            if demo:
                an = demo_analyst(k, prices[-1][1])
            elif it["region"] in ("Krypto", "Rohstoffe", "ETFs"):
                an = None
            else:
                an = fetch_analyst(it["ticker"])
                time.sleep(0.2)
            n_an += an is not None
            results.append(analyse(it, prices, cur, fx, cfg, an))
        except Exception as e:
            print(f"FEHLER Analyse {it['name']}: {e}")
            failed.append(it["name"])

    print(f"Analysten-Daten für {n_an} Werte geladen.")
    above = [r for r in results if r["ind"]["dist200"] is not None]
    breadth = 100 * sum(r["ind"]["dist200"] > 0 for r in above) / len(above) if above else 0
    counts = {k: sum(r["status"] == k for r in results) for k in ("BUY", "SELL", "HOLD", "WAIT")}
    data = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "date": max(r["date"] for r in results),
        "params": {k: cfg[k] for k in ("fast_sma", "slow_sma", "stop_loss_pct", "fee_per_order_eur")},
        "market": {"breadth": breadth, "avg_score": sum(r["score"] for r in results) / len(results),
                   "counts": counts, "total": len(items), "failed": failed},
        "items": results,
    }
    os.makedirs(os.path.join(HERE, "site", "d"), exist_ok=True)
    for r in results:
        with open(os.path.join(HERE, "site", "d", f"{r['id']}.json"), "w", encoding="utf-8") as f:
            json.dump(r.pop("_detail"), f, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(HERE, "site", "data.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

    sig = sorted((r for r in results if r["status"] in ("BUY", "SELL")), key=lambda r: -r["score"])
    hold = [r for r in results if r["status"] == "HOLD"]
    lines = []
    for r in sig:
        verb = "Kaufsignal" if r["status"] == "BUY" else "Trend gekippt (nur relevant, wenn du sie hast)"
        lines.append(f"{'🟢' if r['status'] == 'BUY' else '🔴'} {verb}: {r['name']} {fmt(r['price'], r['cur'])} · Score {r['score']}")
    if not sig:
        lines.append("Heute keine neuen Kauf- oder Verkaufssignale.")
    lines.append(f"Halten: {len(hold)} · Marktbreite {breadth:.0f} % über 200-Tage-Linie")
    msg = "\n".join(lines)
    print(msg)
    if failed:
        print("Nicht geladen:", ", ".join(failed))
    for r in results:
        p = r["rec"]
        print(f"WERT {r['id']} {r['status']} score={r['score']} preis={r['price']:.2f}{r['cur']} "
              f"stop={p.get('stop') or 0:.2f} d200={r['ind']['dist200'] if r['ind']['dist200'] is None else round(r['ind']['dist200'], 1)} "
              f"rsi={r['ind']['rsi']:.0f} r3m={r['ind']['r3m'] if r['ind']['r3m'] is None else round(r['ind']['r3m'], 1)}")

    app = cfg["app_url"]
    nb = sum(r["status"] == "BUY" for r in sig)
    push = {
        "send": bool(sig) or not cfg.get("only_send_on_signal"),
        "title": f"Signal-Radar: {nb}× Kaufsignal, {len(sig) - nb}× Trend gekippt" if sig else "Signal-Radar: Tagesupdate",
        "message": msg,
        "click": f"{app}#{sig[0]['id']}" if sig else app,
        "priority": "high" if sig else "low",
        "tags": "rotating_light" if sig else "bar_chart",
        "actions": f"view, App öffnen, {app}",
    }
    with open(os.path.join(HERE, "push.json"), "w", encoding="utf-8") as f:
        json.dump(push, f, ensure_ascii=False)
    if not dry and "--no-push" not in args:
        notify(os.path.join(HERE, "push.json"))


def notify(path):
    """Schickt die vorbereitete Push-Nachricht (läuft im Workflow nach dem App-Update)."""
    with open(path, encoding="utf-8") as f:
        p = json.load(f)
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print("WARNUNG: Secret NTFY_TOPIC fehlt – keine Push-Nachricht (siehe ANLEITUNG.md).")
        return
    if not p["send"]:
        print("Kein neues Signal – keine Push-Nachricht.")
        return
    ntfy(topic, p["title"], p["message"], click=p["click"], priority=p["priority"],
         tags=p["tags"], actions=p["actions"])
    print("Push gesendet.")

if __name__ == "__main__":
    main()

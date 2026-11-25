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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import learn

HERE = os.path.dirname(os.path.abspath(__file__))
REGION = {"sp500": "USA", "usa": "USA", "europa": "Europa", "deutschland": "Deutschland", "asien": "Asien",
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
               f"{urllib.parse.quote(ticker)}?range={years}y&interval=1d&events=div")
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
    divs = []
    for v in (res.get("events", {}).get("dividends") or {}).values():
        try:
            divs.append((datetime.fromtimestamp(int(v["date"]), tz=timezone.utc).date(), float(v["amount"])))
        except Exception:
            pass
    return rows, res["meta"].get("currency") or "EUR", sorted(divs)


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


# ---------------------------------------------------------------- Nachrichten (Yahoo Finance Suche)
def fetch_news(query, n=5):
    url = ("https://query2.finance.yahoo.com/v1/finance/search?"
           + urllib.parse.urlencode({"q": query, "quotesCount": 0, "newsCount": n, "enableFuzzyQuery": "false"}))
    op, _ = _yahoo_session()
    try:
        if op:
            data = json.load(op.open(url, timeout=20))
        else:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            data = json.load(urllib.request.urlopen(req, timeout=20))
    except Exception:
        return []
    out = []
    for it in data.get("news", [])[:n]:
        link = it.get("link") or ""
        if not link.startswith("https://"):
            continue
        out.append({"t": (it.get("title") or "")[:160], "p": it.get("publisher") or "",
                    "u": link, "ts": int(it.get("providerPublishTime") or 0)})
    return out


DEMO_HEADLINES = [
    "Zinsentscheid der Notenbank sorgt für Nervosität an den Märkten",
    "Quartalszahlen über den Erwartungen, Ausblick vorsichtig",
    "Analyst senkt Kursziel nach Branchenschwäche",
    "Handelsstreit: Neue Zölle belasten Exportwerte",
    "Ölpreis steigt nach Förderkürzung",
]


def demo_news(seed, n=3):
    rnd = random.Random(seed * 13)
    now = int(time.time())
    return [{"t": rnd.choice(DEMO_HEADLINES), "p": "Testquelle", "u": "https://finance.yahoo.com/",
             "ts": now - rnd.randint(600, 86400)} for _ in range(n)]


def trading_time(r):
    t = r["ticker"]
    if r["region"] == "Krypto":
        return "Rund um die Uhr handelbar – Limit-Order jederzeit möglich."
    if t.endswith((".HK", ".T", ".KS")):
        return "Heimatbörse handelt nachts. Bei Trade Republic 7:30–23:00 Uhr, aber nur mit Limit-Order (Kurse schwanken stärker)."
    if "." in t or t.endswith("=F"):
        return "Europa: Am besten ab 9:30 Uhr (nach der unruhigen ersten halben Stunde) mit Limit-Order."
    return "USA: Am besten ab 16:00 Uhr – 30 Min. nach US-Börsenstart (15:30) ist der Kurs fairer. Limit-Order nutzen."


def find_dips(results, cfg, model):
    """Dip-Chancen – nur Setups, die bei dieser Aktie historisch funktioniert haben."""
    out = []
    for r in results:
        if r.get("flags"):
            continue
        s = r["_series"]
        i = len(s["closes"]) - 1
        f = learn.dip_setup(i, s["closes"], s["s200"], s["rsis"])
        if not f:
            continue
        st = r["dip_stats"]
        p = learn.smoothed(st, model, f)
        g_avg = model["all"]["avg_ret"] or 0
        exp = ((st["avg_ret"] or 0) * st["n"] + learn.PRIOR * g_avg) / (st["n"] + learn.PRIOR)
        if p < 52 or exp <= 0:
            continue
        price = r["price"]
        limit, stop, target = learn.dip_plan(price, s["sma20"][-1], cfg["stop_loss_pct"])
        an = r.get("analyst")
        bull = bool(an and an.get("rec_mean") and an["rec_mean"] <= 2.3 and an["upside"] >= 15)
        d1, d5 = r["ind"].get("r1d") or 0, r["ind"].get("r5d") or 0
        reasons = [(f"Minus {abs(d1):.1f} % heute" if d1 <= -2.5 else f"Minus {abs(d5):.1f} % in 5 Tagen").replace(".", ","),
                   "langfristig im Aufwärtstrend (über der 200-Tage-Linie, Plus über 12 Monate)"]
        if st["n"]:
            reasons.append(f"Bei {r['name']} ging dieses Setup in {st['wins']} von {st['n']} Fällen auf "
                           f"(Ø {st['avg_ret']:+.1f} % in {st['avg_days']:.0f} Tagen)".replace(".", ","))
        if r["ind"]["rsi"] < 35:
            reasons.append(f"RSI {r['ind']['rsi']:.0f}: überverkauft")
        if bull:
            reasons.append(f"Analysten: „{an['label']}“, Ø Kursziel {an['upside']:+.0f} %")
        score = p + (5 if bull else 0) + (3 if st["n"] >= 12 else 0) - (8 if r["ind"]["vol"] > 60 else 0)
        out.append({
            "id": r["id"], "name": r["name"], "cur": r["cur"], "date": r["date"],
            "score": int(max(0, min(99, round(score)))), "p": round(p, 1), "n": st["n"],
            "avg_ret": st["avg_ret"], "avg_days": st["avg_days"], "exp": exp,
            "drop": f["drop"], "limit": limit, "stop": stop, "rebound": target,
            "target": an["mean"] if an else None,
            "rr": (target - limit) / (limit - stop) if limit > stop else None,
            "reasons": reasons, "when": trading_time(r),
        })
    return sorted(out, key=lambda x: -x["score"])[:15]


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
    divs = [] if rnd.random() < 0.4 else [(out[i][0], out[i][1] * 0.006) for i in range(len(out) - 1, 0, -63)][:8]
    return out, rnd.choice(["EUR", "EUR", "USD", "CHF", "GBp"]), sorted(divs)


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
def simulate(dates, closes, fast, slow, cfg, s200=None):
    stop_pct = cfg["stop_loss_pct"] / 100
    fee_frac = 2 * cfg["fee_per_order_eur"] / cfg["position_size_eur"]
    start = cfg["slow_sma"]
    trades, events, pos, equity, curve = [], [], None, 1.0, []
    for i in range(start, len(closes)):
        c, d = closes[i], dates[i]
        up = fast[i - 1] <= slow[i - 1] and fast[i] > slow[i]
        dn = fast[i - 1] >= slow[i - 1] and fast[i] < slow[i]
        if pos is None and up and (not cfg.get("trend_filter") or (s200 and s200[i] and c > s200[i])):
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
    stats["_curve"] = curve
    stats["_start"] = start
    return trades, events, pos, stats


# ---------------------------------------------------------------- Strategie je Aktie optimieren
PAIRS = [(10, 30), (20, 50), (30, 100), (50, 200)]
STOPS = [6, 8, 12]


def _window_ret(st, closes, a, b):
    """Rendite der Strategie zwischen Tag a und b (aus der Equity-Kurve)."""
    curve, start = st["_curve"], st["_start"]
    ia, ib = max(a - start, 0), min(b - start, len(curve) - 1)
    if ib <= ia:
        return None
    return (curve[ib] / curve[ia] - 1) * 100


def optimize(dates, closes, s200, cfg):
    """Sucht die beste Einstellung auf 70 % der Daten und prüft sie auf den letzten 30 % (ungesehen)."""
    default = {"fast": cfg["fast_sma"], "slow": cfg["slow_sma"], "stop": cfg["stop_loss_pct"], "filt": False}
    n = len(closes)
    if n < 700:
        return {**default, "adopted": False, "note": "zu kurze Kursgeschichte – Standard-Einstellung"}
    split = int(n * 0.7)
    smas = {p: sma(closes, p) for pair in PAIRS for p in pair}
    def run(f, s, stop, filt):
        c2 = {**cfg, "fast_sma": f, "slow_sma": s, "stop_loss_pct": stop, "trend_filter": filt}
        return simulate(dates, closes, smas[f], smas[s], c2, s200)[3]
    best, best_score = None, -1e9
    for f, s in PAIRS:
        for stop in STOPS:
            for filt in (False, True):
                st = run(f, s, stop, filt)
                r = _window_ret(st, closes, 0, split)
                if r is None:
                    continue
                score = r / (1 + abs(min(0, learn_dd(st["_curve"][:max(1, split - st["_start"])]))) / 25)
                if score > best_score:
                    best, best_score = (f, s, stop, filt, st), score
    if not best:
        return {**default, "adopted": False, "note": "Optimierung nicht möglich – Standard-Einstellung"}
    f, s, stop, filt, st = best
    oos = _window_ret(st, closes, split, n - 1)
    dst = run(default["fast"], default["slow"], default["stop"], False)
    oos_def = _window_ret(dst, closes, split, n - 1)
    oos_bh = (closes[-1] / closes[split] - 1) * 100
    adopted = oos is not None and oos_def is not None and oos > oos_def and oos > 0
    years_oos = (dates[-1] - dates[split]).days / 365.25
    chosen = {"fast": f, "slow": s, "stop": stop, "filt": filt} if adopted else default
    return {**chosen, "adopted": adopted, "oos": oos, "oos_default": oos_def, "oos_bh": oos_bh,
            "oos_years": years_oos, "tested": {"fast": f, "slow": s, "stop": stop, "filt": filt},
            "note": ("Optimierte Einstellung – auch auf ungesehenen Daten besser als Standard" if adopted
                     else "Optimierung brachte auf ungesehenen Daten keinen Vorteil – Standard-Einstellung")}


def learn_dd(curve):
    return max_drawdown(curve) if curve else 0


# ---------------------------------------------------------------- Analyse eines Werts
def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def analyse(item, prices, cur, fx, cfg, analyst=None, strat=None, divs=None):
    dates = [p[0] for p in prices]
    closes = [p[1] for p in prices]
    s200 = sma(closes, 200)
    strat = strat or optimize(dates, closes, s200, cfg)
    cfg = {**cfg, "fast_sma": strat["fast"], "slow_sma": strat["slow"], "stop_loss_pct": strat["stop"],
           "trend_filter": strat["filt"]}
    fast, slow = sma(closes, cfg["fast_sma"]), sma(closes, cfg["slow_sma"])
    trades, events, pos, st = simulate(dates, closes, fast, slow, cfg, s200)
    st.pop("_curve"); st.pop("_start")
    today, price = dates[-1], closes[-1]
    flags = learn.validate(dates, closes, item["region"])
    if analyst and analyst.get("mean") and not (0.25 < analyst["mean"] / price < 4):
        analyst = None   # Kursziel passt nicht zum Kurs (z. B. andere Währung) – lieber weglassen

    lookback = cfg.get("alert_lookback_days", 3)
    recent = [e for e in events if (today - e["date"]).days <= lookback]
    last = recent[-1] if recent else None
    status = last["type"] if last else ("HOLD" if pos else "WAIT")
    if flags:
        status = "WAIT"

    macd_line = [a - b for a, b in zip(ema(closes, 12), ema(closes, 26))]
    macd_sig = ema(macd_line, 9)
    macd_hist = macd_line[-1] - macd_sig[-1]
    logrets = [math.log(closes[i] / closes[i - 1]) for i in range(max(1, len(closes) - 63), len(closes))]
    vol = statistics.pstdev(logrets) * math.sqrt(252) * 100 if len(logrets) > 2 else 0
    year = closes[-252:]
    div_ttm = sum(a for d, a in (divs or []) if (dates[-1] - d).days <= 365)
    last_div = max(divs, key=lambda x: x[0]) if divs else None
    ind = {
        "rsi": rsi(closes[-300:]),
        "macd_hist_pct": macd_hist / price * 100,
        "dist200": (price / s200[-1] - 1) * 100 if s200[-1] else None,
        "slope50": (slow[-1] / slow[-21] - 1) * 100 if len(slow) > 21 and slow[-21] else None,
        "gap": (fast[-1] / slow[-1] - 1) * 100,
        "r1d": ret(closes, 1), "r5d": ret(closes, 5), "r1m": ret(closes, 21), "r3m": ret(closes, 63), "r1y": ret(closes, 252),
        "vol": vol,
        "dd1y": max_drawdown(year),
        "from_high": (price / max(year) - 1) * 100,
        "div_ttm": div_ttm or None, "div_yield": (div_ttm / price * 100) if div_ttm else None,
        "div_last": str(last_div[0]) if last_div else None, "div_last_amt": last_div[1] if last_div else None,
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

    if strat.get("adopted"):
        add("+", f"Für {item['name']} optimierte Regel: SMA {strat['fast']}/{strat['slow']}, Stop {strat['stop']} %"
                 + (", nur über der 200-Tage-Linie" if strat["filt"] else "")
                 + f". Auf {strat['oos_years']:.1f} Jahren ungesehener Daten: {strat['oos']:+.0f} % statt {strat['oos_default']:+.0f} % mit Standard.")
    elif strat.get("oos") is not None:
        add("~", f"Standard-Regel (SMA {strat['fast']}/{strat['slow']}): Eine Optimierung brachte auf ungesehenen Daten keinen Vorteil.")
    for fl in flags:
        why.insert(0, {"t": "-", "x": f"Datenprüfung: {fl}. Deshalb keine Empfehlung für diesen Wert."})

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
    if flags:
        rec["text"] = f"Keine Empfehlung – {flags[0]}. Erst handeln, wenn die Daten wieder stimmen."
        rec["watch"] = False

    # ---- Chartdaten (letzte ~260 Tage täglich + bis 5 Jahre wöchentlich)
    n0 = max(0, len(closes) - 260)
    rnd = lambda x: None if x is None else float(f"{x:.5g}")
    opens = [p[2] for p in prices]
    highs = [p[3] for p in prices]
    lows = [p[4] for p in prices]
    vols = [p[5] for p in prices]
    rsis = rsi_series(closes)
    sma20 = sma(closes, 20)
    dip_samples = learn.learn_history(closes, highs, lows, s200, sma20, rsis, cfg["stop_loss_pct"])
    for smp in dip_samples:
        smp["region"] = item["region"]
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
        "status": status, "score": score, "analyst": an,
        "ind": {**ind, "sup_pct": (sup / price - 1) * 100, "res_pct": (resi / price - 1) * 100 if resi else None},
        "stats": st, "rec": rec, "spark": spark, "flags": flags,
        "strat": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in strat.items()},
        "dip_stats": learn.summarize(dip_samples),
        "_samples": dip_samples,
        "_series": {"dates": dates, "closes": closes, "highs": highs, "lows": lows, "s200": s200,
                    "sma20": sma20, "rsis": rsis},
        "_detail": {"chart": chart, "trades": trades[-10:][::-1], "why": why, "parts": parts, "parts_max": maxes},
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
def rsig(o, sig=4):
    """Zahlen auf sinnvolle Stellen runden (kleinere Datei, gleiche Aussage)."""
    if isinstance(o, float):
        return float(f"{o:.{sig}g}")
    if isinstance(o, dict):
        return {k: rsig(v, sig) for k, v in o.items()}
    if isinstance(o, list):
        return [rsig(v, sig) for v in o]
    return o


def fetch_prev(app_url):
    """Letzten veröffentlichten Stand der App laden (für Vergleich, Analysten-Cache und Protokoll)."""
    def get(name):
        req = urllib.request.Request(app_url + name + f"?t={int(time.time())}", headers={"User-Agent": "signal-radar"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    prev = None
    try:
        prev = get("data.json")
    except Exception as e:
        print(f"Vorheriger Stand nicht ladbar: {e}")
    try:
        journal = get("journal.json")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise SystemExit(f"Protokoll nicht ladbar ({e}) – Abbruch, damit nichts verloren geht.")
        journal = {}
    except Exception as e:
        raise SystemExit(f"Protokoll nicht ladbar ({e}) – Abbruch, damit nichts verloren geht.")
    return prev, journal


def main():
    args = set(sys.argv[1:])
    if "--notify" in args:
        return notify(os.path.join(HERE, "push.json"))
    demo = "--demo" in args
    dry = demo or "--print" in args
    mode = os.environ.get("RUN_MODE", "full")
    cfg = load_json("config.json")
    items = watchlist(cfg)
    t0 = time.time()

    prev, journal = (None, {}) if demo else fetch_prev(cfg["app_url"])
    prev_items = {r["id"]: r for r in (prev or {}).get("items", [])}

    # ---- Kurse parallel laden
    def load(k_it):
        k, it = k_it
        try:
            prices, cur, divs = demo_prices(k) if demo else fetch_prices(it["ticker"], cfg.get("history_years", 5))
            if len(prices) < cfg["slow_sma"] + 15:
                raise ValueError(f"nur {len(prices)} Kurstage")
            return it, (prices, divs), cur, None
        except Exception as e:
            return it, None, None, e
    raw, failed = [], []
    with ThreadPoolExecutor(max_workers=1 if demo else 8) as ex:
        for it, prices, cur, err in ex.map(load, enumerate(items)):
            if err:
                print(f"FEHLER {it['name']} ({it['ticker']}): {err}")
                failed.append(it["name"])
            else:
                raw.append((it, prices, cur))
    if len(raw) < len(items) * 0.5:
        raise SystemExit(f"Nur {len(raw)} von {len(items)} Werten geladen – Abbruch, alte Daten bleiben sichtbar.")
    print(f"Kurse: {len(raw)}/{len(items)} in {time.time() - t0:.0f}s")
    if demo:
        learn.TODAY = max(p[0][-1][0] for _, p, _ in raw)

    # ---- Analysten: im Voll-Lauf neu, im 10-Minuten-Lauf aus dem letzten Stand
    fx = fx_rates({cur for _, _, cur in raw}, demo)
    def analyst_for(k, it, prices):
        if demo:
            return demo_analyst(k, prices[-1][1])
        if it["region"] in ("Krypto", "Rohstoffe", "ETFs"):
            return None
        rid = re.sub(r"[^A-Za-z0-9]+", "-", it["ticker"]).strip("-")
        old = prev_items.get(rid, {}).get("analyst")
        if mode == "quick" and prev:
            return {k2: old[k2] for k2 in ("n", "mean", "high", "low", "rec", "rec_mean", "cur", "votes")} if old else None
        return fetch_analyst(it["ticker"])
    if not demo and (mode != "quick" or not prev):
        _yahoo_session()
    with ThreadPoolExecutor(max_workers=1 if demo else 6) as ex:
        ans = list(ex.map(lambda a: analyst_for(*a), [(k, it, p[0]) for k, (it, p, _) in enumerate(raw)]))
    print(f"Analysten-Daten für {sum(a is not None for a in ans)} Werte ({'aus Cache' if mode == 'quick' and prev else 'neu geladen'}).")

    results = []
    t1 = time.time()
    for (it, prices, cur), an in zip(raw, ans):
        try:
            rid = re.sub(r"[^A-Za-z0-9]+", "-", it["ticker"]).strip("-")
            old = prev_items.get(rid, {}).get("strat")
            strat = old if (mode == "quick" and old and "fast" in old) else None
            results.append(analyse(it, prices[0], cur, fx, cfg, an, strat, prices[1]))
        except Exception as e:
            print(f"FEHLER Analyse {it['name']}: {e}")
            failed.append(it["name"])
    print(f"Analyse in {time.time() - t1:.0f}s · optimierte Regel übernommen bei "
          f"{sum(1 for r in results if r['strat'].get('adopted'))} von {len(results)} Werten")
    flagged = [r["name"] for r in results if r["flags"]]
    if flagged:
        print("Datenprüfung – ausgeschlossen:", ", ".join(flagged))

    # ---- Lernen: was hat über alle Aktien funktioniert?
    all_samples = [s for r in results for s in r.pop("_samples")]
    model = learn.global_model(all_samples)
    print(f"Gelernt aus {model['all']['n']} historischen Dip-Fällen: Trefferquote {model['all']['win_rate'] or 0:.0f} %, "
          f"Ø {model['all']['avg_ret'] or 0:+.2f} %")
    dips = find_dips(results, cfg, model)
    by = {r["id"]: r for r in results}

    # ---- Nachrichten
    prev_ch = {c["id"] for c in (prev or {}).get("chances", [])}
    movers = sorted(results, key=lambda r: -abs(r["ind"].get("r1d") or 0))[:8]
    want = [d["id"] for d in dips] + [m["id"] for m in movers]
    if mode == "quick" and prev:
        for rid in want:
            if rid in prev_items and prev_items[rid].get("news") and rid in prev_ch:
                by[rid]["news"] = prev_items[rid]["news"]
    def news_for(rid):
        r = by[rid]
        q = r["ticker"] if not r["ticker"].endswith(("-EUR", "=F")) else r["name"]
        return rid, fetch_news(q, 4)
    todo = [rid for rid in dict.fromkeys(want) if "news" not in by[rid]]
    if demo:
        for k, rid in enumerate(todo):
            by[rid]["news"] = demo_news(k)
    else:
        with ThreadPoolExecutor(max_workers=4) as ex:
            for rid, n in ex.map(news_for, todo):
                by[rid]["news"] = n
    if demo:
        world = demo_news(99, 6)
    elif mode == "quick" and prev and prev.get("world") and datetime.now(timezone.utc).minute >= 10:
        world = prev["world"]
    else:
        world, seen = [], set()
        for q in ("SPY", "^GSPC", "QQQ", "^GDAXI", "GLD", "^TNX", "stock market"):
            for n in fetch_news(q, 5):
                if n["u"] not in seen and n["t"]:
                    seen.add(n["u"])
                    world.append(n)
        world = sorted(world, key=lambda n: -n["ts"])[:10]

    # ---- Live-Protokoll fortschreiben
    today_str = str(max(r["date"] for r in results))
    journal = learn.update_journal(journal or {}, dips, {r["id"]: r["_series"] for r in results}, today_str)
    for r in results:
        r.pop("_series")
    js = journal["summary"]
    print(f"Chancen: {len(dips)} · Protokoll: {js['total']} Einträge, {js['closed']} abgeschlossen")

    above = [r for r in results if r["ind"]["dist200"] is not None]
    breadth = 100 * sum(r["ind"]["dist200"] > 0 for r in above) / len(above) if above else 0
    counts = {k: sum(r["status"] == k for r in results) for k in ("BUY", "SELL", "HOLD", "WAIT")}
    hour = datetime.now(timezone.utc).hour
    run_kind = "live" if mode == "quick" else ("morgen" if hour < 11 else "us" if hour < 17 else "abend")
    data = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "date": today_str, "run": run_kind, "mode": mode,
        "params": {k: cfg[k] for k in ("fast_sma", "slow_sma", "stop_loss_pct", "fee_per_order_eur")},
        "market": {"breadth": breadth, "avg_score": sum(r["score"] for r in results) / len(results),
                   "counts": counts, "total": len(items), "failed": failed, "flagged": flagged},
        "model": model, "journal": js,
        "items": results, "chances": dips, "world": world,
    }
    os.makedirs(os.path.join(HERE, "site", "d"), exist_ok=True)
    for r in results:
        with open(os.path.join(HERE, "site", "d", f"{r['id']}.json"), "w", encoding="utf-8") as f:
            json.dump(r.pop("_detail"), f, ensure_ascii=False, separators=(",", ":"))
    for r in results:
        for key in ("ind", "stats", "dip_stats", "strat"):
            r[key] = rsig(r[key], 4)
        if r.get("analyst"):
            r["analyst"] = {k: (rsig(v, 4) if k in ("upside", "rec_mean", "buy_share") else v) for k, v in r["analyst"].items()}
        r["eur_rate"] = rsig(r["eur_rate"], 6)
    data["model"] = rsig(data["model"], 4)
    data["chances"] = rsig(data["chances"], 6)
    with open(os.path.join(HERE, "site", "data.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(HERE, "site", "journal.json"), "w", encoding="utf-8") as f:
        json.dump(journal, f, ensure_ascii=False, separators=(",", ":"))

    # ---- Push: nur Neues (im Vergleich zum letzten veröffentlichten Stand)
    prev_status = {rid: p.get("status") for rid, p in prev_items.items()}
    sig = sorted((r for r in results if r["status"] in ("BUY", "SELL")), key=lambda r: -r["score"])
    new_sig = [r for r in sig if prev_status.get(r["id"]) != r["status"]] if prev else sig
    new_ch = [d for d in dips if d["id"] not in prev_ch] if prev else dips
    evening = run_kind == "abend"
    show_sig, show_ch = (sig, dips) if evening else (new_sig, new_ch)
    lines = []
    for r in show_sig:
        verb = "Kaufsignal" if r["status"] == "BUY" else "Trend gekippt (nur relevant, wenn du sie hast)"
        lines.append(f"{'🟢' if r['status'] == 'BUY' else '🔴'} {verb}: {r['name']} {fmt(r['price'], r['cur'])} · Score {r['score']}")
    for d in show_ch[:5]:
        r = by[d["id"]]
        lines.append(f"💡 Dip-Chance: {r['name']} ({format(d['drop'], '+.1f').replace('.', ',')} %) · Limit {fmt(d['limit'], r['cur'])}"
                     f" · historisch {d['p']:.0f} % Treffer")
    if evening:
        lines.append(f"Marktbreite {breadth:.0f} % über 200-Tage-Linie · {len(results)} Werte geprüft")
    msg = "\n".join(lines) or "Keine neuen Signale."
    print(msg)
    app = cfg["app_url"]
    nb = sum(r["status"] == "BUY" for r in show_sig)
    label = {"live": "Signal-Radar live", "morgen": "Signal-Radar am Morgen", "us": "Signal-Radar zur US-Eröffnung",
             "abend": "Signal-Radar"}[run_kind]
    if show_ch and not show_sig:
        title = f"{label}: {len(show_ch)} neue Dip-Chance{'n' if len(show_ch) != 1 else ''}"
    elif show_sig:
        title = f"{label}: {nb}× Kaufsignal, {len(show_sig) - nb}× Trend gekippt"
    else:
        title = f"{label}: Tagesupdate"
    push = {
        "send": (bool(new_sig or new_ch) if not evening else (bool(sig or dips) or not cfg.get("only_send_on_signal"))),
        "title": title, "message": msg,
        "click": f"{app}#{show_sig[0]['id']}" if show_sig else (f"{app}#chancen" if show_ch else app),
        "priority": "high" if (new_sig or new_ch) else "low",
        "tags": "rotating_light" if show_sig else ("bulb" if show_ch else "bar_chart"),
        "actions": f"view, App öffnen, {app}",
    }
    # Ereignisse je Aktie – der Push-Schritt filtert sie auf dein Depot (Secret DEPOT)
    events = []
    chance_ids = {d["id"] for d in dips}
    for r in results:
        p = prev_items.get(r["id"])
        if not p:
            continue
        name, ind, pind = r["name"], r["ind"], p.get("ind", {})
        if r["status"] == "SELL" and p.get("status") != "SELL":
            events.append((r["id"], f"🔴 {name}: Verkaufssignal ({r['rec'].get('reason', 'Trendwende')})"))
        if r["status"] == "BUY" and p.get("status") != "BUY":
            events.append((r["id"], f"🟢 {name}: neues Kaufsignal (Nachkauf möglich)"))
        if (pind.get("dist200") or 0) > 0 and ind.get("dist200") is not None and ind["dist200"] <= 0:
            events.append((r["id"], f"⚠️ {name}: fällt unter die 200-Tage-Linie – langfristiger Trend kippt"))
        if (ind.get("r1d") or 0) <= -5 and ((pind.get("r1d") or 0) > -5 or p.get("date") != r["date"]):
            events.append((r["id"], f"📉 {name}: {format(ind['r1d'], '.1f').replace('.', ',')} % heute"))
        if (ind.get("r1d") or 0) >= 6 and ((pind.get("r1d") or 0) < 6 or p.get("date") != r["date"]):
            events.append((r["id"], f"📈 {name}: +{format(ind['r1d'], '.1f').replace('.', ',')} % heute"))
        pa, na = p.get("analyst") or {}, r.get("analyst") or {}
        if pa.get("label") and na.get("label") and pa["label"] != na["label"]:
            events.append((r["id"], f"🧭 {name}: Analysten jetzt „{na['label']}“ (vorher „{pa['label']}“)"))
        if r["id"] in chance_ids and r["id"] not in prev_ch:
            events.append((r["id"], f"💡 {name}: Dip-Chance zum Nachkaufen"))
        if r["flags"] and not p.get("flags"):
            events.append((r["id"], f"❔ {name}: Datenproblem – Empfehlung pausiert"))
    push["events"] = [{"id": i, "text": t} for i, t in events]
    with open(os.path.join(HERE, "push.json"), "w", encoding="utf-8") as f:
        json.dump(push, f, ensure_ascii=False)
    print(f"Fertig in {time.time() - t0:.0f}s ({mode}).")
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
    # Depot-Alarme: nur für Werte aus dem Secret DEPOT (wird nirgends gespeichert oder ausgegeben)
    mine = {re.sub(r"[^A-Za-z0-9]+", "-", x.strip()).strip("-").upper()
            for x in os.environ.get("DEPOT", "").split(",") if x.strip()}
    hits = [e for e in p.get("events", []) if e["id"].upper() in mine]
    if hits:
        app = p["click"].split("#")[0]
        ntfy(topic, f"Dein Depot: {len(hits)} Hinweis{'e' if len(hits) > 1 else ''}",
             "\n".join(e["text"] for e in hits), click=f"{app}#{hits[0]['id']}", priority="high",
             tags="briefcase", actions=f"view, App öffnen, {app}")
        print(f"Depot-Alarm gesendet ({len(hits)}).")
    elif mine:
        print("Depot: keine neuen Hinweise.")
    if not p["send"]:
        print("Kein neues Signal – keine Push-Nachricht.")
        return
    ntfy(topic, p["title"], p["message"], click=p["click"], priority=p["priority"],
         tags=p["tags"], actions=p["actions"])
    print("Push gesendet.")

if __name__ == "__main__":
    main()

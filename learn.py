"""
Lernen, Prüfen, Protokollieren
==============================
1. Datenprüfung: veraltete Kurse und unplausible Sprünge werden erkannt.
2. Historisches Lernen: Jedes Dip-Setup wird an der Kursgeschichte jeder Aktie
   nachgespielt (gleiche Regeln wie live: Limit, Stop-Loss, Erholungsziel).
   Daraus entstehen Trefferquoten je Aktie und je Merkmal.
3. Live-Protokoll: Jede gezeigte Chance wird festgehalten und weiterverfolgt
   (ausgeführt? Ziel oder Stop erreicht?). So sieht man ehrlich, wie gut der Bot liegt.

Alle Dip-Regeln stehen nur hier, damit Live-Anzeige und Lernen exakt gleich rechnen.
"""
from datetime import date, datetime, timezone

LIMIT_DISCOUNT = 0.985    # Limit-Kauf 1,5 % unter Schlusskurs
TARGET_MIN = 1.02         # Erholungsziel mindestens 2 % über Limit
TARGET_DEFAULT = 1.08     # sonst 8 % über Limit
MAX_DAYS = 20             # nach 20 Handelstagen wird abgerechnet
FILL_DAYS = 2             # Limit muss innerhalb von 2 Tagen greifen
MIN_SAMPLES = 6           # so viele historische Fälle braucht eine Aktie mindestens
PRIOR = 6                 # Glättung: so stark zählt der Gesamtdurchschnitt mit
TODAY = None              # nur für Tests: festes "heute"


# ---------------------------------------------------------------- Datenprüfung
def validate(dates, closes, region, today=None):
    """Gibt eine Liste von Warnungen zurück. Leere Liste = Daten in Ordnung."""
    flags = []
    today = today or TODAY or datetime.now(timezone.utc).date()
    age = (today - dates[-1]).days
    if region != "Krypto" and age > 5:
        flags.append(f"Kurs veraltet (letzter Kurs vom {dates[-1]:%d.%m.%Y})")
    if region == "Krypto" and age > 2:
        flags.append(f"Kurs veraltet (letzter Kurs vom {dates[-1]:%d.%m.%Y})")
    if any(c <= 0 for c in closes[-300:]):
        flags.append("Ungültige Kurse (null oder negativ)")
    limit = 0.6 if region == "Krypto" else 0.35
    tail = closes[-300:]
    for i in range(1, len(tail)):
        if abs(tail[i] / tail[i - 1] - 1) > limit:
            flags.append("Unplausibler Kurssprung – mögliche Datenlücke oder Aktiensplit")
            break
    return flags


# ---------------------------------------------------------------- Dip-Regel (eine Quelle für live und Lernen)
def dip_setup(i, closes, s200, rsis):
    """Ist Tag i ein Dip in einem intakten Aufwärtstrend? Gibt Merkmale oder None zurück."""
    if i < 252 or s200[i] is None or rsis[i] is None:
        return None
    d1 = closes[i] / closes[i - 1] - 1
    d5 = closes[i] / closes[i - 5] - 1
    if not (d5 <= -0.04 or (d1 <= -0.025 and rsis[i] < 35)):
        return None
    if not (closes[i] > s200[i] * 0.94 and closes[i] > closes[i - 252]):
        return None
    drop = min(d1, d5) * 100
    return {
        "drop": drop,
        "rsi_b": "unter 30" if rsis[i] < 30 else "30–40" if rsis[i] < 40 else "über 40",
        "drop_b": "über 10 %" if drop <= -10 else "6–10 %" if drop <= -6 else "4–6 %",
    }


def dip_plan(close, sma20, stop_pct):
    limit = close * LIMIT_DISCOUNT
    stop = limit * (1 - stop_pct / 100)
    target = sma20 if sma20 and sma20 > limit * TARGET_MIN else limit * TARGET_DEFAULT
    return limit, stop, target


def play_out(start, limit, stop, target, highs, lows, closes):
    """Spielt eine Order ab Tag `start` durch. Gibt (Ergebnis, Rendite %, Tage) oder None (noch offen)."""
    n = len(closes)
    fill = None
    for j in range(start, min(start + FILL_DAYS, n)):
        if lows[j] <= limit:
            fill = j
            break
    if fill is None:
        return ("nicht ausgeführt", 0.0, 0) if start + FILL_DAYS <= n else None
    for k in range(fill, min(fill + MAX_DAYS, n)):
        if lows[k] <= stop:                       # vorsichtig: Stop zuerst prüfen
            return ("verloren", (stop / limit - 1) * 100, k - fill)
        if highs[k] >= target:
            return ("gewonnen", (target / limit - 1) * 100, k - fill)
    if fill + MAX_DAYS <= n:
        r = (closes[fill + MAX_DAYS - 1] / limit - 1) * 100
        return ("gewonnen" if r > 0 else "verloren", r, MAX_DAYS)
    return None


def learn_history(closes, highs, lows, s200, sma20, rsis, stop_pct):
    """Alle historischen Dip-Fälle einer Aktie mit Ergebnis."""
    out, skip = [], 0
    for i in range(252, len(closes) - 1):
        if i < skip:
            continue
        f = dip_setup(i, closes, s200, rsis)
        if not f:
            continue
        limit, stop, target = dip_plan(closes[i], sma20[i], stop_pct)
        res = play_out(i + 1, limit, stop, target, highs, lows, closes)
        if res is None:
            break
        skip = i + 5
        if res[0] == "nicht ausgeführt":
            continue
        out.append({**f, "won": res[0] == "gewonnen", "ret": res[1], "days": res[2]})
    return out


def summarize(samples):
    n = len(samples)
    if not n:
        return {"n": 0, "wins": 0, "win_rate": None, "avg_ret": None, "avg_days": None}
    w = sum(s["won"] for s in samples)
    return {"n": n, "wins": w, "win_rate": 100 * w / n,
            "avg_ret": sum(s["ret"] for s in samples) / n,
            "avg_days": sum(s["days"] for s in samples) / n}


def global_model(all_samples):
    """Was der Bot über alle Aktien gelernt hat – gesamt und je Merkmal."""
    model = {"all": summarize(all_samples), "by": {}}
    for key, label in (("rsi_b", "RSI"), ("drop_b", "Rückgang"), ("region", "Region")):
        groups = {}
        for s in all_samples:
            groups.setdefault(s.get(key, "?"), []).append(s)
        model["by"][label] = {k: summarize(v) for k, v in sorted(groups.items())}
    return model


def smoothed(stock_stats, model, feats):
    """Trefferquote dieser Aktie, geglättet mit dem, was über ähnliche Fälle gelernt wurde."""
    base = model["all"]["win_rate"] or 50.0
    for label, key in (("RSI", "rsi_b"), ("Rückgang", "drop_b")):
        g = model["by"].get(label, {}).get(feats.get(key))
        if g and g["n"] >= 30:
            base = (base + g["win_rate"]) / 2
    n, w = stock_stats["n"], stock_stats["wins"]
    return (w + PRIOR * base / 100) / (n + PRIOR) * 100


# ---------------------------------------------------------------- Live-Protokoll
def update_journal(journal, chances, series_by_id, today_str):
    """Neue Chancen eintragen, offene weiterverfolgen."""
    entries = journal.setdefault("entries", [])
    keys = {e["key"] for e in entries}
    for c in chances:
        key = f"{c['id']}|{c['date']}"
        if key not in keys:
            entries.append({"key": key, "id": c["id"], "name": c["name"], "date": c["date"], "cur": c["cur"],
                            "limit": c["limit"], "stop": c["stop"], "target": c["rebound"],
                            "p": c.get("p"), "status": "offen", "ret": None, "days": None})
            keys.add(key)
    for e in entries:
        if e["status"] not in ("offen", "ausgeführt"):
            continue
        s = series_by_id.get(e["id"])
        if not s:
            continue
        dates = [str(d) for d in s["dates"]]
        # Kursverlauf ab dem Tag nach der Empfehlung
        start = next((i for i, d in enumerate(dates) if d > e["date"]), None)
        if start is None:
            continue
        res = play_out(start, e["limit"], e["stop"], e["target"], s["highs"], s["lows"], s["closes"])
        if res is None:
            filled = any(lo <= e["limit"] for lo in s["lows"][start:start + FILL_DAYS])
            e["status"] = "ausgeführt" if filled else "offen"
            if filled:
                e["ret"] = (s["closes"][-1] / e["limit"] - 1) * 100
            continue
        e["status"], e["ret"], e["days"] = res[0], res[1], res[2]
    entries.sort(key=lambda e: e["date"], reverse=True)
    journal["entries"] = entries[:400]
    closed = [e for e in entries if e["status"] in ("gewonnen", "verloren")]
    journal["summary"] = {
        "total": len(entries),
        "open": sum(e["status"] in ("offen", "ausgeführt") for e in entries),
        "closed": len(closed),
        "won": sum(e["status"] == "gewonnen" for e in closed),
        "not_filled": sum(e["status"] == "nicht ausgeführt" for e in entries),
        "win_rate": 100 * sum(e["status"] == "gewonnen" for e in closed) / len(closed) if closed else None,
        "avg_ret": sum(e["ret"] for e in closed) / len(closed) if closed else None,
        "since": min((e["date"] for e in entries), default=today_str),
    }
    return journal

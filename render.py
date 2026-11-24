"""Zeichnet das Dashboard-Bild und Signal-Charts (PNG, fürs Handy optimiert)."""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

W = 1080
BG, CARD, LINE = "#0a0e14", "#131a24", "#1f2937"
INK, MUTED = "#f1f5f9", "#8b98a9"
GREEN, RED, AMBER, GREY = "#22c55e", "#f43f5e", "#f59e0b", "#64748b"
FAST, SLOW = "#38bdf8", "#a78bfa"
STATUS = {
    "BUY": ("JETZT KAUFEN", GREEN), "SELL": ("JETZT VERKAUFEN", RED),
    "HOLD": ("HALTEN", AMBER), "WAIT": ("WARTEN", GREY),
}
PX = 72 / 100  # Pixel -> Punkt bei dpi=100


def fmt_money(x, cur):
    sym = {"EUR": "€", "USD": "$"}.get(cur, cur)
    s = f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{s} {sym}"


def fmt_pct(x):
    return f"{x:+.1f} %".replace(".", ",")


class Canvas:
    def __init__(self, height):
        self.H = height
        self.fig = plt.figure(figsize=(W / 100, height / 100), dpi=100, facecolor=BG)
        self.ax = self.fig.add_axes([0, 0, 1, 1])
        self.ax.set_xlim(0, W)
        self.ax.set_ylim(height, 0)
        self.ax.axis("off")

    def box(self, x, y, w, h, color, r=26, ec="none"):
        self.ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle=f"round,pad=0,rounding_size={r}",
                                         fc=color, ec=ec, lw=1.5))

    def text(self, x, y, s, size, color=INK, weight="normal", ha="left", va="baseline"):
        self.ax.text(x, y, s, fontsize=size * PX, color=color, fontweight=weight,
                     ha=ha, va=va, family="DejaVu Sans")

    def pill(self, x_right, y, label, color):
        w = len(label) * 14.5 + 44
        self.box(x_right - w, y, w, 48, color, r=24)
        self.text(x_right - w / 2, y + 25, label, 22, "#06110a" if color != GREY else INK,
                  "bold", ha="center", va="center")

    def chart(self, x, y, w, h, r, window=130):
        ax = self.fig.add_axes([x / W, 1 - (y + h) / self.H, w / W, h / self.H])
        ax.set_facecolor("none")
        for s in ax.spines.values():
            s.set_visible(False)
        ax.set_xticks([]), ax.set_yticks([])
        closes, fast, slow = r["closes"][-window:], r["fast"][-window:], r["slow"][-window:]
        n0 = len(r["closes"]) - len(closes)
        xs = range(len(closes))
        color = STATUS[r["status"]][1]
        ax.plot(xs, closes, color=INK, lw=2.4, solid_capstyle="round")
        ax.plot(xs, fast, color=FAST, lw=1.8, alpha=0.95)
        ax.plot(xs, slow, color=SLOW, lw=1.8, alpha=0.95)
        for e in r["events"]:
            i = e["i"] - n0
            if 0 <= i < len(closes):
                buy = e["type"] == "BUY"
                ax.scatter([i], [closes[i]], s=170, marker="^" if buy else "v",
                           color=GREEN if buy else RED, edgecolor=BG, lw=2, zorder=7)
        if r["position"]:
            ax.axhline(r["position"]["stop"], color=RED, lw=1.4, ls=(0, (4, 4)), alpha=0.9)
        ax.scatter([len(closes) - 1], [closes[-1]], s=60, color=INK, zorder=4)
        vals = closes + [v for v in fast + slow if v is not None]
        if r["position"]:
            vals.append(r["position"]["stop"])
        lo, hi = min(vals), max(vals)
        pad = (hi - lo) * 0.08 or 1
        ax.fill_between(xs, closes, lo - pad, color=color, alpha=0.10, lw=0, zorder=0)
        ax.set_ylim(lo - pad, hi + pad)
        ax.set_xlim(-1, len(closes) + 1)

    def save(self, path):
        self.fig.savefig(path, dpi=100, facecolor=BG)
        plt.close(self.fig)


def card(c, y, r, chart_h, cfg):
    x, w = 32, W - 64
    h = chart_h + 250
    c.box(x, y, w, h, CARD)
    label, color = STATUS[r["status"]]
    c.text(x + 36, y + 66, r["name"][:22], 38, INK, "bold")
    c.text(x + 36, y + 108, f"{r['ticker']}  ·  {fmt_money(r['price'], r['cur'])}", 24, MUTED)
    c.pill(x + w - 30, y + 36, label, color)
    c.chart(x + 24, y + 132, w - 48, chart_h, r)
    # Kennzahlen-Zeile
    st, pos = r["stats"], r["position"]
    cells = []
    if pos:
        cells.append(("Stop-Loss", fmt_money(pos["stop"], r["cur"]), RED))
        gain = (r["price"] / pos["entry_price"] - 1) * 100
        cells.append(("Seit Kauf", fmt_pct(gain), GREEN if gain >= 0 else RED))
        cells.append(("Haltedauer", f"{r['held']} / Ø {st['avg_days']:.0f} T", INK))
    elif r["status"] == "SELL":
        cells.append(("Verkaufen bei", fmt_money(r["price"], r["cur"]), RED))
        cells.append(("Grund", r["last"]["reason"], INK))
        cells.append(("Trefferquote", f"{st['win_rate']:.0f} %", INK))
    else:
        cells.append(("Abstand Signal", fmt_pct(r["gap"]), FAST))
        cells.append(("Trefferquote", f"{st['win_rate']:.0f} %", INK))
        cells.append(("Ø Haltedauer", f"{st['avg_days']:.0f} Tage", INK))
    edge = st["strategy_pct"] - st["buy_hold_pct"]
    cells.append(("vs. Halten", fmt_pct(edge), GREEN if edge >= 0 else RED))
    cw = (w - 72) / len(cells)
    for i, (k, v, col) in enumerate(cells):
        cx = x + 36 + i * cw
        c.text(cx, y + chart_h + 180, k, 19, MUTED)
        c.text(cx, y + chart_h + 220, v, 26, col, "bold")
    return h


def dashboard(results, picked, total, failed, date, cfg, path):
    chart_h = 170
    card_h = chart_h + 250
    H = 330 + len(picked) * (card_h + 28) + 150
    c = Canvas(H)
    c.text(48, 92, "Signal-Radar", 58, INK, "bold")
    c.text(50, 138, f"{date:%d.%m.%Y}  ·  {total} Werte beobachtet  ·  SMA {cfg['fast_sma']}/{cfg['slow_sma']}",
           23, MUTED)
    counts = {k: sum(r["status"] == k for r in results) for k in STATUS}
    tiles = [("Kaufen", counts["BUY"], GREEN), ("Verkaufen", counts["SELL"], RED),
             ("Halten", counts["HOLD"], AMBER), ("Warten", counts["WAIT"], GREY)]
    tw = (W - 64 - 3 * 18) / 4
    for i, (k, n, col) in enumerate(tiles):
        tx = 32 + i * (tw + 18)
        c.box(tx, 175, tw, 120, CARD, r=22)
        c.box(tx, 175, 8, 120, col, r=4)
        c.text(tx + 30, 245, str(n), 52, col, "bold")
        c.text(tx + 30, 280, k, 20, MUTED)
    y = 330
    for r in picked:
        y += card(c, y, r, chart_h, cfg) + 28
    legend = [("Kurs", INK), (f"SMA {cfg['fast_sma']}", FAST), (f"SMA {cfg['slow_sma']}", SLOW),
              ("Stop-Loss", RED)]
    lx = 48
    for k, col in legend:
        c.box(lx, y + 16, 26, 8, col, r=4)
        c.text(lx + 36, y + 27, k, 20, MUTED)
        lx += 60 + len(k) * 12
    note = "Regelbasierte Signale, keine Anlageberatung. Verluste möglich."
    if failed:
        note = f"{len(failed)} Werte nicht geladen: {', '.join(failed[:4])}" + ("…" if len(failed) > 4 else "")
    c.text(48, y + 80, note, 20, MUTED)
    c.save(path)


def signal_chart(r, cfg, path):
    chart_h = 420
    c = Canvas(chart_h + 250 + 64)
    card(c, 32, r, chart_h, cfg)
    c.save(path)

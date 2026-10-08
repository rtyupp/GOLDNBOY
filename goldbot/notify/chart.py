"""Chart generated inside the bot (matplotlib, no TradingView screenshot)."""
from __future__ import annotations
import io
from typing import Optional
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import numpy as np
import pandas as pd

BG, FG, GRID = "#0e1117", "#d0d4dc", "#1f2430"
UP, DN = "#26a69a", "#ef5350"


def render_chart(ctx, plan=None, tf: str = "5m", bars: int = 100, title: str = "GOLD XAU/USD") -> bytes:
    st = ctx.tf[tf]
    df = st.df.iloc[-bars:]
    n = len(df)
    off = len(st.df) - n
    fig, ax = plt.subplots(figsize=(12, 7), dpi=110)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    x = np.arange(n)
    o, h, l, c = (df[k].values for k in ("open", "high", "low", "close"))
    for i in range(n):
        col = UP if c[i] >= o[i] else DN
        ax.vlines(i, l[i], h[i], color=col, linewidth=0.9, zorder=3)
        ax.add_patch(Rectangle((i - 0.32, min(o[i], c[i])), 0.64, max(abs(c[i] - o[i]), 1e-6), color=col, zorder=4))
    ax.plot(x, df["ema_20"].values, color="#ffb300", lw=1.1, label="EMA20", zorder=2)
    ax.plot(x, df["ema_50"].values, color="#42a5f5", lw=1.1, label="EMA50", zorder=2)
    ax.plot(x, df["vwap"].values, color="#ab47bc", lw=1.3, ls="--", label="VWAP", zorder=2)
    lo_y, hi_y = float(l.min()), float(h.max())
    # supply / demand zones (best scored, still inside view)
    for z in sorted(st.zones, key=lambda z: -z.score)[:4]:
        zi = z.idx - off
        if z.top < lo_y - 5 or z.bottom > hi_y + 5:
            continue
        col = UP if z.direction == "bull" else DN
        ax.add_patch(Rectangle((max(zi, 0), z.bottom), n - max(zi, 0), z.top - z.bottom, color=col, alpha=0.13, zorder=1))
        ax.text(n - 0.5, z.top, {"demand": "Demand", "supply": "Supply", "fvg_bull": "FVG bull", "fvg_bear": "FVG bear"}.get(z.kind, z.kind) + f" {z.score:.0f}", color=col, fontsize=7, ha="right", va="bottom")
    # support / resistance
    for s in st.support[:2]:
        ax.axhline(s["price"], color="#66bb6a", lw=0.8, ls=":", alpha=0.8)
        ax.text(0, s["price"], " Support", color="#66bb6a", fontsize=7, va="bottom")
    for r in st.resistance[:2]:
        ax.axhline(r["price"], color="#ef5350", lw=0.8, ls=":", alpha=0.8)
        ax.text(0, r["price"], " Resistance", color="#ef5350", fontsize=7, va="bottom")
    # liquidity + session levels
    for name, v in ctx.levels.items():
        if lo_y - 6 <= v <= hi_y + 6:
            ax.axhline(v, color="#8d99ae", lw=0.7, ls="-.", alpha=0.7)
            ax.text(n - 0.5, v, name, color="#8d99ae", fontsize=7, ha="right", va="bottom")
    for s in st.sweeps[:3]:
        si = s.idx - off
        if si >= 0:
            ax.annotate("Liquidity sweep", (si, s.extreme), color="#ffd54f", fontsize=7, ha="center",
                        xytext=(0, 10 if s.expected == "bear" else -14), textcoords="offset points",
                        arrowprops=dict(arrowstyle="-", color="#ffd54f", lw=0.6))
    # BOS / CHoCH
    for b in st.breaks[-5:]:
        bi = b.idx - off
        if bi >= 0:
            ax.hlines(b.level, max(bi - 12, 0), bi, color="#26c6da", lw=0.8)
            ax.text(bi, b.level, ("BOS" if b.kind == "BOS" else "CHoCH"), color="#26c6da", fontsize=7, va="bottom" if b.direction == "bull" else "top")
    # trade plan
    if plan is not None:
        ax.axhline(plan.entry, color="#ffffff", lw=1.2)
        ax.axhline(plan.sl, color=DN, lw=1.2)
        ax.axhline(plan.tp1, color=UP, lw=1.0, ls="--")
        ax.axhline(plan.tp2, color=UP, lw=1.0)
        ax.add_patch(Rectangle((n - 12, min(plan.entry, plan.sl)), 12, abs(plan.entry - plan.sl), color=DN, alpha=0.18, zorder=0))
        ax.add_patch(Rectangle((n - 12, min(plan.entry, plan.tp2)), 12, abs(plan.tp2 - plan.entry), color=UP, alpha=0.14, zorder=0))
        for lab, v, col in (("ENTRY", plan.entry, "#ffffff"), ("SL", plan.sl, DN), ("TP1", plan.tp1, UP), ("TP2", plan.tp2, UP)):
            ax.text(n - 12.5, v, f"{v:.2f}  {lab} ", color=col, fontsize=8, va="bottom", ha="right", fontweight="bold",
                    bbox=dict(facecolor=BG, edgecolor="none", alpha=0.75, pad=1))
        lo_y, hi_y = min(lo_y, plan.sl, plan.tp2), max(hi_y, plan.sl, plan.tp2)
    pad = (hi_y - lo_y) * 0.06
    ax.set_ylim(lo_y - pad, hi_y + pad)
    ax.set_xlim(-1, n + 1)
    step = max(1, n // 8)
    ax.set_xticks(x[::step])
    ax.set_xticklabels([t.strftime("%d %H:%M") for t in df.index[::step]], color=FG, fontsize=8)
    ax.tick_params(colors=FG, labelsize=8)
    ax.grid(color=GRID, lw=0.5)
    for sp in ax.spines.values():
        sp.set_color(GRID)
    ax.yaxis.tick_right()
    head = f"{title}  {tf}  |  {ctx.session['label']}  |  {ctx.ts:%Y-%m-%d %H:%M} UTC"
    ax.set_title(head + (f"  |  {plan.direction}" if plan else ""), color=FG, fontsize=11, loc="left")
    leg = ax.legend(loc="upper left", fontsize=7, facecolor=BG, edgecolor=GRID, labelcolor=FG)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()

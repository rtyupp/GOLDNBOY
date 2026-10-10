"""Confluence score 0..100 = weighted evidence for ONE direction. NOT a probability."""
from __future__ import annotations
from typing import Dict, Tuple
import numpy as np

WEIGHTS = {"Trend": 20, "Structure": 15, "Liquidity": 15, "Momentum": 10, "VWAP": 10,
           "Volatility": 5, "Session": 10, "PriceAction": 15}


def confluence(ctx, d: str, weights: Dict[str, float] | None = None) -> Tuple[float, Dict[str, float]]:
    w = weights or WEIGHTS
    s5, s15, s1h, s4h = ctx.tf["5m"], ctx.tf["15m"], ctx.tf["1H"], ctx.tf["4H"]
    want = "BULLISH" if d == "bull" else "BEARISH"
    parts: Dict[str, float] = {}
    # Trend: 4H + 1H + 15m agreement
    parts["Trend"] = (0.45 * (s4h.trend == want) + 0.35 * (s1h.trend == want) + 0.20 * (s15.trend == want))
    # Structure: recent BOS/CHoCH in direction on 5m/15m, no recent opposite CHoCH
    st = 0.0
    if s5.recent_break(d, 15):
        st += 0.5
    if s15.recent_break(d, 10):
        st += 0.3
    lb = s5.last_break
    if not (lb and lb.kind == "CHoCH" and lb.direction != d and len(s5.df) - 1 - lb.idx <= 15):
        st += 0.2
    parts["Structure"] = min(st, 1.0)
    # Liquidity: confirmed sweep in direction (5m/15m)
    lq = 0.0
    for s in s5.sweeps + s15.sweeps:
        if s.expected == d and s.bars_ago <= 12:
            lq = max(lq, 1.0 if s.confirmed else 0.5)
    parts["Liquidity"] = lq
    # Momentum: RSI side + MACD hist side + stochastic
    l = s5.last
    m = 0.0
    m += 0.4 * ((l["rsi"] > 50) == (d == "bull"))
    m += 0.4 * ((l["macd_hist"] > 0) == (d == "bull"))
    m += 0.2 * ((l["stoch_k"] > l["stoch_d"]) == (d == "bull"))
    parts["Momentum"] = m
    parts["VWAP"] = 1.0 if ((l["close"] > l["vwap"]) == (d == "bull")) else 0.0
    # Volatility: ATR within a normal band relative to its own recent history (not dead, not exploding)
    atr_s = s5.df["atr"].dropna().iloc[-200:]
    pct = float((atr_s < s5.atr).mean()) if len(atr_s) > 20 else 0.5
    parts["Volatility"] = 1.0 if 0.2 <= pct <= 0.85 else 0.3
    lab = ctx.session["label"]
    parts["Session"] = {"London/New York": 1.0, "New York": 0.9, "London": 0.9, "Asian": 0.4}.get(lab, 0.2)
    pa = 0.0
    last = s5.df.iloc[-1]
    body = abs(last["close"] - last["open"]) / max(last["high"] - last["low"], 1e-9)
    pa += 0.5 * (((last["close"] > last["open"]) == (d == "bull")) and body >= 0.5)
    pa += 0.5 * ctx.mtf["dirs"][d]["flags"]["confirm1"]
    parts["PriceAction"] = pa
    total = sum(w[k] * float(v) for k, v in parts.items()) / sum(w.values()) * 100.0
    return round(total, 1), {k: round(float(v), 2) for k, v in parts.items()}

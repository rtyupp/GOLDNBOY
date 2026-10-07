"""Market structure: swings (confirmed only), HH/HL/LH/LL, BOS / CHoCH, S/R, trend state."""
from __future__ import annotations
from dataclasses import dataclass
from typing import List, Optional
import numpy as np
import pandas as pd


@dataclass
class Swing:
    idx: int
    ts: pd.Timestamp
    price: float
    kind: str                 # 'H' or 'L'
    confirmed_idx: int
    broken: bool = False


@dataclass
class BreakEvent:
    kind: str                 # 'BOS' | 'CHoCH'
    direction: str            # 'bull' | 'bear'
    level: float
    idx: int
    ts: pd.Timestamp


def find_swings(df: pd.DataFrame, left: int = 3, right: int = 3) -> List[Swing]:
    """Fractal pivots. A pivot at i is only reported once `right` later bars exist (no repaint)."""
    h, l, n = df["high"].values, df["low"].values, len(df)
    ix = df.index
    raw: List[Swing] = []
    for i in range(left, n - right):
        if h[i] > h[i - left:i].max() and h[i] >= h[i + 1:i + right + 1].max():
            raw.append(Swing(i, ix[i], float(h[i]), "H", i + right))
        if l[i] < l[i - left:i].min() and l[i] <= l[i + 1:i + right + 1].min():
            raw.append(Swing(i, ix[i], float(l[i]), "L", i + right))
    raw.sort(key=lambda s: (s.idx, s.kind))
    out: List[Swing] = []
    for s in raw:                      # enforce alternation: keep the more extreme of consecutive same-kind
        if out and out[-1].kind == s.kind:
            prev = out[-1]
            if (s.kind == "H" and s.price > prev.price) or (s.kind == "L" and s.price < prev.price):
                out[-1] = s
        else:
            out.append(s)
    return out


def _rel(a: float, b: float, tol: float) -> str:
    if a > b + tol:
        return "up"
    if a < b - tol:
        return "down"
    return "eq"


def classify(swings: List[Swing], atr: float) -> dict:
    highs = [s for s in swings if s.kind == "H"]
    lows = [s for s in swings if s.kind == "L"]
    info = {"trend": "RANGE", "hi": None, "lo": None, "last_high": highs[-1] if highs else None,
            "last_low": lows[-1] if lows else None}
    if len(highs) < 2 or len(lows) < 2:
        return info
    tol = 0.15 * max(atr, 1e-9)
    hi = _rel(highs[-1].price, highs[-2].price, tol)
    lo = _rel(lows[-1].price, lows[-2].price, tol)
    info["hi"] = {"up": "HH", "down": "LH", "eq": "EH"}[hi]
    info["lo"] = {"up": "HL", "down": "LL", "eq": "EL"}[lo]
    if (hi, lo) in (("up", "up"), ("up", "eq"), ("eq", "up")):
        info["trend"] = "BULLISH"
    elif (hi, lo) in (("down", "down"), ("down", "eq"), ("eq", "down")):
        info["trend"] = "BEARISH"
    elif (hi, lo) == ("up", "down"):
        info["trend"] = "TRANSITION"        # expansion: both extremes violated
    else:
        info["trend"] = "RANGE"             # contraction / equal
    return info


def detect_breaks(df: pd.DataFrame, swings: List[Swing]) -> List[BreakEvent]:
    """Sequential state machine on closes. BOS = break in trend direction, CHoCH = break against it."""
    close = df["close"].values
    ix = df.index
    sw = sorted(swings, key=lambda s: s.confirmed_idx)
    events: List[BreakEvent] = []
    trend: Optional[str] = None
    last_h: Optional[Swing] = None
    last_l: Optional[Swing] = None
    si = 0
    for i in range(len(df)):
        while si < len(sw) and sw[si].confirmed_idx <= i:
            s = sw[si]
            if s.kind == "H":
                last_h = s
            else:
                last_l = s
            si += 1
        if last_h is not None and not last_h.broken and close[i] > last_h.price:
            last_h.broken = True
            kind = "CHoCH" if trend == "bear" else "BOS"
            events.append(BreakEvent(kind, "bull", last_h.price, i, ix[i]))
            trend = "bull"
        if last_l is not None and not last_l.broken and close[i] < last_l.price:
            last_l.broken = True
            kind = "CHoCH" if trend == "bull" else "BOS"
            events.append(BreakEvent(kind, "bear", last_l.price, i, ix[i]))
            trend = "bear"
    return events


def support_resistance(swings: List[Swing], price: float, atr: float, max_levels: int = 3):
    tol = 0.3 * max(atr, 1e-9)
    levels = []
    for s in swings:
        for lv in levels:
            if abs(lv["price"] - s.price) <= tol:
                lv["touches"] += 1
                lv["price"] = (lv["price"] * (lv["touches"] - 1) + s.price) / lv["touches"]
                break
        else:
            levels.append({"price": s.price, "touches": 1})
    sup = sorted([x for x in levels if x["price"] < price], key=lambda x: -x["price"])[:max_levels]
    res = sorted([x for x in levels if x["price"] > price], key=lambda x: x["price"])[:max_levels]
    return sup, res


def analyze_structure(df: pd.DataFrame, atr: float, left: int = 3, right: int = 3, recent_choch_bars: int = 30) -> dict:
    swings = find_swings(df, left, right)
    info = classify(swings, atr)
    breaks = detect_breaks(df, swings)
    n = len(df)
    last_break = breaks[-1] if breaks else None
    trend = info["trend"]
    # a fresh CHoCH against the swing-based trend means the trend is in question
    if last_break and last_break.kind == "CHoCH" and (n - 1 - last_break.idx) <= recent_choch_bars:
        if (trend == "BULLISH" and last_break.direction == "bear") or (trend == "BEARISH" and last_break.direction == "bull"):
            trend = "TRANSITION"
    price = float(df["close"].iloc[-1])
    sup, res = support_resistance(swings[-20:], price, atr)
    return {"trend": trend, "labels": (info["hi"], info["lo"]), "swings": swings, "breaks": breaks,
            "last_break": last_break, "support": sup, "resistance": res,
            "last_high": info["last_high"], "last_low": info["last_low"]}

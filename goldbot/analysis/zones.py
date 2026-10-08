"""Supply / demand (order blocks), Fair Value Gaps, imbalances - each zone is SCORED, never trusted by existence."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional
import numpy as np
import pandas as pd


@dataclass
class Zone:
    kind: str            # demand | supply | fvg_bull | fvg_bear
    direction: str       # bull | bear
    top: float
    bottom: float
    idx: int
    ts: pd.Timestamp
    impulse: float       # displacement body / ATR
    tests: int = 0
    age: int = 0
    mitigated: bool = False
    imbalance: bool = False
    score: float = 0.0
    notes: List[str] = field(default_factory=list)

    @property
    def mid(self):
        return (self.top + self.bottom) / 2


def _count_tests(h, l, c, z: Zone) -> tuple[int, bool]:
    tests, inside_prev = 0, False
    for k in range(z.idx + 1, len(c)):
        if z.direction == "bull" and c[k] < z.bottom or z.direction == "bear" and c[k] > z.top:
            return tests, True
        inside = l[k] <= z.top and h[k] >= z.bottom
        if inside and not inside_prev:
            tests += 1
        inside_prev = inside
    return tests, False


def detect_zones(df: pd.DataFrame, htf_trend: str, local_trend: str, pools=None, lookback: int = 250,
                 disp_atr: float = 1.3) -> List[Zone]:
    sub = df.iloc[-lookback:] if len(df) > lookback else df
    tsx = sub.index
    o, h, l, c = sub["open"].values, sub["high"].values, sub["low"].values, sub["close"].values
    atr = sub["atr"].values
    n = len(sub)
    zones: List[Zone] = []
    for i in range(3, n):
        a = atr[i]
        if np.isnan(a) or a <= 0:
            continue
        body = abs(c[i] - o[i])
        # --- order block: last opposite candle before a displacement candle
        if body >= disp_atr * a:
            bull = c[i] > o[i]
            for j in range(i - 1, max(i - 6, -1), -1):
                if (bull and c[j] < o[j]) or ((not bull) and c[j] > o[j]):
                    z = Zone("demand" if bull else "supply", "bull" if bull else "bear",
                             float(h[j]), float(l[j]), j, tsx[j], float(body / a))
                    zones.append(z)
                    break
        # --- fair value gap (3-candle imbalance)
        if l[i] > h[i - 2]:
            z = Zone("fvg_bull", "bull", float(l[i]), float(h[i - 2]), i - 1, tsx[i - 1], float(abs(c[i - 1] - o[i - 1]) / a))
            z.imbalance = (c[i - 1] - o[i - 1]) >= disp_atr * a
            zones.append(z)
        elif h[i] < l[i - 2]:
            z = Zone("fvg_bear", "bear", float(l[i - 2]), float(h[i]), i - 1, tsx[i - 1], float(abs(c[i - 1] - o[i - 1]) / a))
            z.imbalance = (o[i - 1] - c[i - 1]) >= disp_atr * a
            zones.append(z)
    out = []
    a_last = float(atr[-1]) if not np.isnan(atr[-1]) else 1.0
    for z in zones:
        z.tests, z.mitigated = _count_tests(h, l, c, z)
        if z.mitigated:
            continue
        z.age = n - 1 - z.idx
        z.score = _score(z, htf_trend, local_trend, a_last, pools)
        out.append(z)
    # dedupe overlapping same-kind zones: keep highest score
    out.sort(key=lambda z: -z.score)
    kept: List[Zone] = []
    for z in out:
        if not any(k.kind == z.kind and min(k.top, z.top) > max(k.bottom, z.bottom) for k in kept):
            kept.append(z)
    return kept


def _score(z: Zone, htf: str, local: str, a: float, pools) -> float:
    s = 0.0
    s += min(z.impulse, 3.0) / 3.0 * 30                       # strength of the move that left the zone
    s += {0: 25, 1: 15, 2: 6}.get(z.tests, 0)                  # freshness (times tested)
    s += max(3.0, 15.0 * (1 - z.age / 300.0)) if z.age < 300 else 3.0   # age
    want = "BULLISH" if z.direction == "bull" else "BEARISH"
    opp = "BEARISH" if z.direction == "bull" else "BULLISH"
    s += 15 if htf == want else (0 if htf == opp else 7)       # HTF alignment
    s += 5 if local == want else 0                             # general trend
    # nearby liquidity: a pool just beyond the far edge (swept to create the zone) or ahead as a target
    if pools:
        edge = z.bottom if z.direction == "bull" else z.top
        near = any(abs(p.price - edge) <= 1.5 * a and ((z.direction == "bull" and p.side == "sell") or (z.direction == "bear" and p.side == "buy")) for p in pools)
        s += 10 if near else 0
    if z.kind.startswith("fvg") and not z.imbalance:
        s *= 0.8
    z.notes = [f"impulse={z.impulse:.1f}ATR", f"tests={z.tests}", f"age={z.age}"]
    return round(min(s, 100.0), 1)


def zones_near(zones: List[Zone], price: float, atr: float, max_dist_atr: float = 1.0, min_score: float = 40) -> List[Zone]:
    res = []
    for z in zones:
        if z.score < min_score:
            continue
        dist = 0.0 if z.bottom <= price <= z.top else min(abs(price - z.top), abs(price - z.bottom))
        if dist <= max_dist_atr * atr:
            res.append(z)
    return sorted(res, key=lambda z: -z.score)

"""Liquidity engine: pools (swing / equal / PDH-PDL-PWH-PWL / session levels), sweeps,
stop hunts, failed breakouts. A sweep is NOT a breakout: it needs candle confirmation."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, List, Optional
import pandas as pd


@dataclass
class Pool:
    price: float
    side: str            # 'buy' (resting above highs) | 'sell' (resting below lows)
    kind: str            # swing | equal | PDH | PDL | PWH | PWL | ASIA_H ...
    valid_from: int      # bar index from which the level is known (-1 = always known)
    strength: int = 1


@dataclass
class Sweep:
    kind: str            # sweep | failed_breakout
    pool_kind: str
    level: float
    extreme: float       # wick extreme (invalidation for the trade)
    expected: str        # 'bull' (sell-side swept) | 'bear' (buy-side swept)
    idx: int
    bars_ago: int
    depth: float
    stop_hunt: bool
    confirmed: bool
    confirm_idx: Optional[int]


def build_pools(swings, atr: float, extra_levels: Dict[str, float], recent: int = 30, tol_atr: float = 0.15) -> List[Pool]:
    pools: List[Pool] = []
    tol = tol_atr * max(atr, 1e-9)
    sw = swings[-recent:]
    for s in sw:
        pools.append(Pool(s.price, "buy" if s.kind == "H" else "sell", "swing", s.confirmed_idx))
    # equal highs / lows
    for kind, side in (("H", "buy"), ("L", "sell")):
        pts = [s for s in sw if s.kind == kind]
        for i in range(len(pts)):
            for j in range(i + 1, len(pts)):
                if abs(pts[i].price - pts[j].price) <= tol:
                    p = max(pts[i].price, pts[j].price) if kind == "H" else min(pts[i].price, pts[j].price)
                    pools.append(Pool(p, side, "equal", max(pts[i].confirmed_idx, pts[j].confirmed_idx), 2))
    for name, price in extra_levels.items():
        if price is None or pd.isna(price):
            continue
        side = "buy" if name.endswith(("H", "_H")) or name in ("PDH", "PWH") else "sell"
        pools.append(Pool(float(price), side, name, -1))
    return pools


def detect_sweeps(df: pd.DataFrame, pools: List[Pool], atr: float, lookback: int = 8, confirm_bars: int = 4) -> List[Sweep]:
    o, h, l, c = df["open"].values, df["high"].values, df["low"].values, df["close"].values
    n = len(df)
    out: List[Sweep] = []
    a = max(atr, 1e-9)
    start = max(1, n - lookback)
    for i in range(start, n):
        rng = max(h[i] - l[i], 1e-9)
        for p in pools:
            if p.valid_from >= i:
                continue
            if p.side == "buy":
                depth = h[i] - p.price
                wick = h[i] - max(o[i], c[i])
                if depth >= 0.05 * a and c[i] < p.price and wick / rng >= 0.3:
                    out.append(_mk(df, "sweep", p, h[i], "bear", i, depth, wick, a, confirm_bars))
                elif c[i] > p.price and h[i] - p.price >= 0.05 * a:
                    # candidate breakout: failed if a later close returns below the level
                    for j in range(i + 1, min(n, i + 1 + 3)):
                        if c[j] < p.price:
                            out.append(_mk(df, "failed_breakout", p, h[i:j + 1].max(), "bear", j, h[i:j + 1].max() - p.price, 0.0, a, confirm_bars))
                            break
            else:
                depth = p.price - l[i]
                wick = min(o[i], c[i]) - l[i]
                if depth >= 0.05 * a and c[i] > p.price and wick / rng >= 0.3:
                    out.append(_mk(df, "sweep", p, l[i], "bull", i, depth, wick, a, confirm_bars))
                elif c[i] < p.price and p.price - l[i] >= 0.05 * a:
                    for j in range(i + 1, min(n, i + 1 + 3)):
                        if c[j] > p.price:
                            out.append(_mk(df, "failed_breakout", p, l[i:j + 1].min(), "bull", j, p.price - l[i:j + 1].min(), 0.0, a, confirm_bars))
                            break
    # keep the most recent / deepest per (expected, pool level)
    best: Dict[tuple, Sweep] = {}
    for s in out:
        k = (s.expected, round(s.level, 2))
        if k not in best or s.idx > best[k].idx:
            best[k] = s
    return sorted(best.values(), key=lambda s: (s.bars_ago, -s.depth))


def _mk(df, kind, p: Pool, extreme, expected, idx, depth, wick, atr, confirm_bars) -> Sweep:
    n = len(df)
    c, h, l = df["close"].values, df["high"].values, df["low"].values
    confirmed, cidx = False, None
    ref = idx
    for k in range(ref + 1, min(n, ref + 1 + confirm_bars)):
        if expected == "bear" and c[k] < l[ref]:
            confirmed, cidx = True, k
            break
        if expected == "bull" and c[k] > h[ref]:
            confirmed, cidx = True, k
            break
    if kind == "failed_breakout":     # the closing back inside already is the rejection
        confirmed, cidx = True, idx
    return Sweep(kind, p.kind, float(p.price), float(extreme), expected, int(idx), int(n - 1 - idx),
                 float(depth), bool(depth >= 0.3 * atr or wick >= 0.5 * atr), confirmed, cidx)

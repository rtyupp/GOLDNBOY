"""Builds one AnalysisContext from closed candles of all timeframes (same code for live + backtest)."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional
import numpy as np
import pandas as pd

from goldbot.analysis.indicators import add_indicators
from goldbot.analysis import structure as st, liquidity as lq, zones as zn, sessions as ss, levels as lv
from goldbot.analysis.news import NewsStatus

ANALYZED = ["1m", "5m", "15m", "1H", "4H"]


@dataclass
class TFState:
    tf: str
    df: pd.DataFrame
    trend: str
    labels: tuple
    swings: list
    breaks: list
    last_break: Optional[st.BreakEvent]
    support: list
    resistance: list
    pools: List[lq.Pool]
    sweeps: List[lq.Sweep]
    zones: List[zn.Zone]
    atr: float
    last: pd.Series

    def recent_break(self, direction: str, bars: int) -> Optional[st.BreakEvent]:
        n = len(self.df)
        for b in reversed(self.breaks):
            if n - 1 - b.idx > bars:
                break
            if b.direction == direction:
                return b
        return None


@dataclass
class Ctx:
    ts: pd.Timestamp
    bid: float
    ask: float
    spread: Optional[float]
    tf: Dict[str, TFState]
    levels: Dict[str, float]
    session: dict
    mtf: dict
    news: NewsStatus
    atr: float                          # reference ATR (15m)
    regime: str
    sessions_cfg: dict
    frames: Dict[str, pd.DataFrame] = field(default_factory=dict)

    @property
    def mid(self):
        return (self.bid + self.ask) / 2.0


def _bias_dir(trend: str) -> Optional[str]:
    return "bull" if trend == "BULLISH" else ("bear" if trend == "BEARISH" else None)


class Analyzer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.sessions = cfg.get("sessions", ss.DEFAULT_SESSIONS) or ss.DEFAULT_SESSIONS
        self.swing_lr = int(cfg.get("analysis.swing_lr", 3))
        self.use_ta = bool(cfg.get("analysis.use_ta_library", True))
        self._ind_cache: Dict[tuple, pd.DataFrame] = {}
        self._state_cache: Dict[tuple, TFState] = {}

    # indicators are causal -> computed on the full closed frame; cache by (tf, last close)
    def indicators(self, tf: str, frame: pd.DataFrame) -> pd.DataFrame:
        if frame.empty or "rsi" in frame.columns:      # already has (causal) indicators, e.g. backtest
            return frame
        key = (tf, frame.index[-1], len(frame))
        hit = self._ind_cache.get(key)
        if hit is not None:
            return hit
        out = add_indicators(frame.iloc[-1500:], self.use_ta)
        if len(self._ind_cache) > 40:
            self._ind_cache.clear()
        self._ind_cache[key] = out
        return out

    def tf_state(self, tf: str, df: pd.DataFrame, extra: Dict[str, float], htf_trend: str) -> Optional[TFState]:
        if len(df) < 60 or df["atr"].iloc[-1] != df["atr"].iloc[-1]:
            return None
        key = (tf, df.index[-1], round(sum(extra.values()), 2) if extra else 0, htf_trend)
        hit = self._state_cache.get(key)
        if hit is not None:
            return hit
        d = df.iloc[-300:]
        atr = float(d["atr"].iloc[-1])
        s = st.analyze_structure(d, atr, self.swing_lr, self.swing_lr)
        pools = lq.build_pools(s["swings"], atr, extra)
        sweeps = lq.detect_sweeps(d, pools, atr)
        zones = zn.detect_zones(d, htf_trend, s["trend"], pools)
        state = TFState(tf, d, s["trend"], s["labels"], s["swings"], s["breaks"], s["last_break"],
                        s["support"], s["resistance"], pools, sweeps, zones, atr, d.iloc[-1])
        if len(self._state_cache) > 60:
            self._state_cache.clear()
        self._state_cache[key] = state
        return state

    def analyze(self, frames: Dict[str, pd.DataFrame], bid: float, ask: float, as_of: pd.Timestamp,
                news: NewsStatus) -> tuple[Optional[Ctx], str]:
        ind = {tf: self.indicators(tf, frames[tf]) for tf in ["1m", "5m", "15m", "30m", "1H", "4H", "1D"] if tf in frames}
        for tf in ANALYZED:
            if tf not in ind or len(ind[tf]) < 60:
                return None, f"INSUFFICIENT_HISTORY({tf}:{len(ind.get(tf, []))})"
        extra = dict(lv.htf_levels(ind.get("1D", frames.get("1D"))))
        completed = ss.completed_session_levels(ind["15m"], as_of, self.sessions)
        extra.update(completed)
        # structure of the HTFs first (zones need the HTF trend)
        states: Dict[str, TFState] = {}
        htf_trend = "RANGE"
        for tf in ["4H", "1H", "15m", "5m", "1m"]:
            s = self.tf_state(tf, ind[tf], extra, htf_trend)
            if s is None:
                return None, f"INSUFFICIENT_HISTORY({tf})"
            states[tf] = s
            if tf == "1H":
                htf_trend = s.trend if states["4H"].trend == s.trend else (states["4H"].trend if s.trend in ("RANGE", "TRANSITION") else "RANGE")
        sess = ss.session_info(ind["5m"], as_of, self.sessions, int(self.cfg.get("sessions_or_minutes", 30)), completed)
        mtf = mtf_analysis(states)
        regime = states["1H"].trend
        ctx = Ctx(as_of, bid, ask, ask - bid if ask >= bid else None, states, extra, sess, mtf, news,
                  states["15m"].atr, regime, self.sessions, frames=ind)
        return ctx, "OK"


def mtf_analysis(states: Dict[str, TFState]) -> dict:
    t4, t1 = states["4H"].trend, states["1H"].trend
    d4, d1 = _bias_dir(t4), _bias_dir(t1)
    if d4 and d1 and d4 == d1:
        bias, strength = d4, "STRONG"
    elif d4 and d1 and d4 != d1:
        bias, strength = "conflict", "NONE"
    elif d4 and not d1:
        bias, strength = d4, "WEAK"
    elif d1 and not d4:
        bias, strength = d1, "WEAK"
    else:
        bias, strength = "neutral", "NONE"
    out = {"bias": bias, "strength": strength, "labels": {}, "dirs": {}}
    s15, s5, s1 = states["15m"], states["5m"], states["1m"]
    for d in ("bull", "bear"):
        flags = {}
        want = "BULLISH" if d == "bull" else "BEARISH"
        l15, l5 = s15.last, s5.last
        if d == "bull":
            flags["pullback15"] = bool(l15["close"] < l15["ema_20"] or l15["rsi"] < 50 or s15.trend in ("BEARISH", "RANGE"))
            flags["trend15"] = s15.trend == want
        else:
            flags["pullback15"] = bool(l15["close"] > l15["ema_20"] or l15["rsi"] > 50 or s15.trend in ("BULLISH", "RANGE"))
            flags["trend15"] = s15.trend == want
        flags["sweep5"] = any(s.expected == d and s.bars_ago <= 10 for s in s5.sweeps)
        flags["structure5"] = s5.recent_break(d, 12) is not None
        l1 = s1.last
        flags["confirm1"] = s1.recent_break(d, 10) is not None or bool(
            (d == "bull" and l1["close"] > l1["ema_9"] and l1["close"] > s1.df["high"].iloc[-2]) or
            (d == "bear" and l1["close"] < l1["ema_9"] and l1["close"] < s1.df["low"].iloc[-2]))
        score = 0
        if bias == d:
            score += 50 if strength == "STRONG" else 28
        elif bias in ("neutral",):
            score += 10
        score += 12 if (flags["pullback15"] or flags["trend15"]) else 0
        score += 15 if (flags["sweep5"] or flags["structure5"]) else 0
        score += 23 if flags["confirm1"] else 0
        if bias == "conflict" or (bias in ("bull", "bear") and bias != d):
            score = min(score, 20)
        out["dirs"][d] = {"flags": flags, "score": min(score, 100)}
    out["labels"] = {tf: states[tf].trend for tf in ("4H", "1H", "15m", "5m", "1m")}
    return out

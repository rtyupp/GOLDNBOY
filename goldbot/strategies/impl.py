"""Five independent strategies. Each returns Signal/Score/Entry/SL/TP1/TP2/Reason via Strategy.evaluate()."""
from __future__ import annotations
from typing import Optional, Tuple
import numpy as np
from goldbot.strategies.base import Strategy, Checks, opp


def _bias_ok(ctx, d: str) -> bool:
    b = ctx.mtf["bias"]
    return b == d or b == "neutral"


def _bullish_close_break(df, d: str) -> bool:
    c, o = df["close"].iloc[-1], df["open"].iloc[-1]
    if d == "bull":
        return bool(c > o and c > df["high"].iloc[-2])
    return bool(c < o and c < df["low"].iloc[-2])


def _last_swing_inval(state, d: str, lookback: int = 12) -> Optional[float]:
    df = state.df
    if d == "bull":
        lows = [s.price for s in state.swings if s.kind == "L"]
        cand = [lows[-1]] if lows else []
        cand.append(float(df["low"].iloc[-lookback:].min()))
        return min(cand)
    highs = [s.price for s in state.swings if s.kind == "H"]
    cand = [highs[-1]] if highs else []
    cand.append(float(df["high"].iloc[-lookback:].max()))
    return max(cand)


class TrendPullback(Strategy):
    key, label = "trend_pullback", "Trend Pullback"

    def check(self, ctx, d):
        s5, s15 = ctx.tf["5m"], ctx.tf["15m"]
        df5 = s5.df
        l5 = s5.last
        ch = Checks()
        mtf = ctx.mtf
        ch.add("HTF trend", mtf["bias"] == d and mtf["strength"] in ("STRONG", "WEAK"), 20, True)
        win = df5.iloc[-6:]
        a5 = s5.atr
        if d == "bull":
            touched = float(win["low"].min()) <= float(l5["ema_20"]) + 0.2 * a5
            intact = float(l5["close"]) > float(l5["ema_50"])
            vwap_ok = float(l5["close"]) > float(l5["vwap"]) or float(win["low"].min()) <= float(l5["vwap"]) + 0.3 * a5 and float(l5["close"]) > float(l5["vwap"])
        else:
            touched = float(win["high"].max()) >= float(l5["ema_20"]) - 0.2 * a5
            intact = float(l5["close"]) < float(l5["ema_50"])
            vwap_ok = float(l5["close"]) < float(l5["vwap"])
        ch.add("pullback to EMA", touched and intact, 15, True)
        ch.add("VWAP", vwap_ok, 10)
        lb = s5.last_break
        broken_against = bool(lb and lb.kind == "CHoCH" and lb.direction == opp(d) and len(df5) - 1 - lb.idx <= 15)
        ch.add("structure intact", (not broken_against) and ctx.tf["1H"].trend != ("BEARISH" if d == "bull" else "BULLISH"), 15, True)
        ch.add("confirmation", _bullish_close_break(df5, d) or ctx.mtf["dirs"][d]["flags"]["confirm1"], 25, True)
        rsi, mh = float(l5["rsi"]), float(l5["macd_hist"])
        mh_prev = float(df5["macd_hist"].iloc[-2])
        mom = (40 <= rsi <= 68 and mh > mh_prev) if d == "bull" else (32 <= rsi <= 60 and mh < mh_prev)
        ch.add("momentum", mom, 10)
        from goldbot.analysis.zones import zones_near
        zs = [z for z in zones_near(s5.zones + s15.zones, ctx.mid, a5, 1.0, 50) if z.direction == d]
        ch.add("zone support", bool(zs), 5)
        return ch, _last_swing_inval(s5, d), {"setup": "Trend Pullback"}


class LiquiditySweep(Strategy):
    key, label = "liquidity_sweep", "Liquidity Sweep"

    def check(self, ctx, d):
        ch = Checks()
        cands = []
        for tf in ("5m", "15m"):
            for sw in ctx.tf[tf].sweeps:
                if sw.expected == d and sw.bars_ago <= (10 if tf == "5m" else 6):
                    cands.append((tf, sw))
        cands.sort(key=lambda x: (not x[1].confirmed, x[1].bars_ago))
        tf, sw = cands[0] if cands else (None, None)
        ch.add("liquidity sweep", sw is not None, 25, True)
        ch.add("candle confirmation", bool(sw and sw.confirmed), 20, True)
        shift = False
        if sw is not None:
            for t2 in ("5m", "1m"):
                st2 = ctx.tf[t2]
                for b in reversed(st2.breaks):
                    if b.direction == d:
                        shift = True if (t2 == "1m" or b.idx >= (sw.idx if tf == "5m" else len(st2.df) - 12)) else shift
                        break
        ch.add("CHoCH/BOS after sweep", shift, 20, True)
        ch.add("HTF not opposing", ctx.mtf["bias"] in (d, "neutral") or (ctx.mtf["bias"] != "conflict" and ctx.mtf["strength"] == "WEAK"), 15, True)
        ch.add("rejection / stop-hunt", bool(sw and sw.stop_hunt), 10)
        ch.add("quality pool", bool(sw and sw.pool_kind != "swing"), 10)
        inval = sw.extreme if sw else None
        return ch, inval, {"setup": "Liquidity Sweep", "sweep": (sw.pool_kind, round(sw.level, 2)) if sw else None}


class Breakout(Strategy):
    key, label = "breakout", "Breakout"

    def check(self, ctx, d):
        s15, s5 = ctx.tf["15m"], ctx.tf["5m"]
        df = s15.df
        a = s15.atr
        ch = Checks()
        rng = df.iloc[-26:-6]
        rh, rl = float(rng["high"].max()), float(rng["low"].min())
        ch.add("range / compression", (rh - rl) <= 4.0 * a, 15, True)
        recent = df.iloc[-6:]
        level = rh if d == "bull" else rl
        if d == "bull":
            idxs = [i for i in range(len(recent)) if float(recent["close"].iloc[i]) > rh]
        else:
            idxs = [i for i in range(len(recent)) if float(recent["close"].iloc[i]) < rl]
        ch.add("breakout close", bool(idxs), 20, True)
        vol_ok = bool(idxs) and float(recent["rel_vol"].iloc[idxs[0]]) >= 1.3
        ch.add("volume (tick) expansion", vol_ok, 15)
        retest, rt_ext = False, None
        if idxs:
            after = recent.iloc[idxs[0] + 1:]
            if len(after):
                if d == "bull":
                    retest = float(after["low"].min()) <= level + 0.3 * a and float(after["close"].iloc[-1]) > level
                    rt_ext = min(level, float(after["low"].min()))
                else:
                    retest = float(after["high"].max()) >= level - 0.3 * a and float(after["close"].iloc[-1]) < level
                    rt_ext = max(level, float(after["high"].max()))
        ch.add("retest holds", retest, 20, True)
        ch.add("confirmation", _bullish_close_break(s5.df, d), 20, True)
        ch.add("HTF not opposing", ctx.mtf["bias"] in (d, "neutral"), 10)
        return ch, rt_ext, {"setup": "Breakout", "level": round(level, 2)}


class Reversal(Strategy):
    key, label = "reversal", "Reversal"

    def check(self, ctx, d):
        s15, s5 = ctx.tf["15m"], ctx.tf["5m"]
        df = s15.df
        ch = Checks()
        last = df.iloc[-6:]
        if d == "bull":
            ext = float(df["rsi"].iloc[-1]) <= 32 or float(last["rsi"].min()) <= 30 or bool((last["close"] < last["bb_low"]).any())
        else:
            ext = float(df["rsi"].iloc[-1]) >= 68 or float(last["rsi"].max()) >= 70 or bool((last["close"] > last["bb_high"]).any())
        ch.add("extreme", ext, 20, True)
        sw = [s for s in s5.sweeps + s15.sweeps if s.expected == d and s.bars_ago <= 12]
        ch.add("liquidity", bool(sw), 20, True)
        kind = "H" if d == "bear" else "L"
        pts = [s for s in s15.swings if s.kind == kind][-2:]
        div = False
        if len(pts) == 2:
            r0, r1 = float(df["rsi"].iloc[pts[0].idx]), float(df["rsi"].iloc[pts[1].idx])
            if d == "bear":
                div = pts[1].price > pts[0].price and r1 < r0
            else:
                div = pts[1].price < pts[0].price and r1 > r0
        ch.add("divergence", div, 20, True)
        shift = s5.recent_break(d, 12) is not None
        ch.add("structure shift (CHoCH/BOS)", shift, 25, True)
        strong_against = ctx.mtf["bias"] == opp(d) and ctx.mtf["strength"] == "STRONG"
        ch.add("not against strong HTF", not strong_against, 15, True)
        inval = (sw[0].extreme if sw else None)
        return ch, inval, {"setup": "Reversal"}


class VwapStrategy(Strategy):
    key, label = "vwap", "VWAP"

    def check(self, ctx, d):
        s5, s15 = ctx.tf["5m"], ctx.tf["15m"]
        df = s5.df
        l = s5.last
        a = s5.atr
        ch = Checks()
        slope_up = float(df["ema_50"].iloc[-1]) > float(df["ema_50"].iloc[-6])
        side_ok = (float(l["close"]) > float(l["vwap"]) and slope_up) if d == "bull" else (float(l["close"]) < float(l["vwap"]) and not slope_up)
        ch.add("trend", side_ok and _bias_ok(ctx, d), 20, True)
        win = df.iloc[-8:]
        if d == "bull":
            retest = float(win["low"].min()) <= float(l["vwap"]) + 0.25 * a and float(l["close"]) > float(l["vwap"])
            inval = min(float(win["low"].min()), float(l["vwap"]) - 0.5 * a)
        else:
            retest = float(win["high"].max()) >= float(l["vwap"]) - 0.25 * a and float(l["close"]) < float(l["vwap"])
            inval = max(float(win["high"].max()), float(l["vwap"]) + 0.5 * a)
        ch.add("VWAP retest", retest, 30, True)
        mh, mh0, rsi = float(l["macd_hist"]), float(df["macd_hist"].iloc[-2]), float(l["rsi"])
        mom = (mh > 0 and mh > mh0 and 50 <= rsi <= 72) if d == "bull" else (mh < 0 and mh < mh0 and 28 <= rsi <= 50)
        ch.add("momentum", mom, 20, True)
        l15 = s15.last
        ch.add("15m side of EMA50", (float(l15["close"]) > float(l15["ema_50"])) == (d == "bull"), 15)
        ch.add("confirmation", _bullish_close_break(df, d), 15)
        return ch, inval, {"setup": "VWAP"}


ALL = [TrendPullback, LiquiditySweep, Breakout, Reversal, VwapStrategy]

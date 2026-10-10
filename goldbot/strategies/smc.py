"""استراتيجيتان من مفاهيم Smart Money (SMC/ICT) مبنيتان على أدوات GOLDNBOY الموجودة
(swings / BOS-CHoCH / sweeps / zones) دون استبدال أي منها ودون تكرار منطق الاستراتيجيات الخمس القائمة.

المفاهيم مأخوذة من الأدبيات العامة (تعريفات FVG وOrder Block وOTE وMSS)، والتنفيذ كتابة مستقلة بالكامل:
لم يُنسخ أي كود من المستودعات الأربعة المقترحة (لا يظهر فيها ترخيص صريح يسمح بالنسخ).

1) SmcOteFvg  — ساق حركة (impulse) ثم ارتداد إلى منطقة OTE (62%-79%) تتقاطع مع فجوة FVG، مع كسر هيكل وشمعة تأكيد.
2) SmcSweepMssFvg — سحب سيولة ثم تغيّر هيكل (MSS) ينتج فجوة FVG، والدخول عند عودة السعر إلى الفجوة.

كل الإشارات سببية: تستخدم فقط شموعًا مغلقة ونقاط swing مؤكدة (انظر analysis/structure.py).
"""
from __future__ import annotations
from typing import Optional

from goldbot.strategies.base import Strategy, Checks

OTE_LO, OTE_HI = 0.62, 0.79
MIN_LEG_ATR, MAX_LEG_ATR = 1.5, 8.0
LEG_MAX_AGE_BARS = 40


def _htf_ok(ctx, d: str) -> bool:
    return ctx.mtf["bias"] in (d, "neutral")


def _confirm_candle(df, d: str) -> bool:
    """شمعة 5 دقائق في اتجاه الصفقة تغلق فوق/تحت منتصف الشمعة السابقة."""
    c, o = float(df["close"].iloc[-1]), float(df["open"].iloc[-1])
    mid_prev = (float(df["high"].iloc[-2]) + float(df["low"].iloc[-2])) / 2.0
    return (c > o and c > mid_prev) if d == "bull" else (c < o and c < mid_prev)


def _overlap(top: float, bottom: float, band_top: float, band_bottom: float) -> bool:
    return min(top, band_top) > max(bottom, band_bottom)


def impulse_leg(state, d: str) -> Optional[dict]:
    """آخر ساق حركة في اتجاه d: (low→high) للصاعد أو (high→low) للهابط، من swings مؤكدة فقط.
    تُرفض الساق إذا: صغيرة/ضخمة جدًا نسبة إلى ATR، قديمة، أو كُسرت نقطة بدايتها (إغلاق خلفها)."""
    sw, df = state.swings, state.df
    n = len(df)
    if d == "bull":
        ends = [s for s in sw if s.kind == "H"]
        if not ends:
            return None
        end = ends[-1]
        starts = [s for s in sw if s.kind == "L" and s.idx < end.idx]
        if not starts:
            return None
        start = starts[-1]
        lo, hi = start.price, end.price
        broken = float(df["close"].iloc[end.idx:].min()) < lo
    else:
        ends = [s for s in sw if s.kind == "L"]
        if not ends:
            return None
        end = ends[-1]
        starts = [s for s in sw if s.kind == "H" and s.idx < end.idx]
        if not starts:
            return None
        start = starts[-1]
        hi, lo = start.price, end.price
        broken = float(df["close"].iloc[end.idx:].max()) > hi
    size = hi - lo
    a = max(state.atr, 1e-9)
    if broken or not (MIN_LEG_ATR * a <= size <= MAX_LEG_ATR * a) or (n - 1 - end.idx) > LEG_MAX_AGE_BARS:
        return None
    return {"lo": lo, "hi": hi, "size": size, "start_idx": start.idx, "end_idx": end.idx,
            "start_ts": df.index[start.idx]}


def ote_band(leg: dict, d: str):
    """(أعلى، أدنى) منطقة OTE بين 62% و79% من الساق."""
    if d == "bull":
        return leg["hi"] - OTE_LO * leg["size"], leg["hi"] - OTE_HI * leg["size"]
    return leg["lo"] + OTE_HI * leg["size"], leg["lo"] + OTE_LO * leg["size"]


def in_ote(state, leg: dict, d: str) -> bool:
    """عمق الارتداد في آخر 3 شموع وصل إلى OTE (62%..~85%) والإغلاق الحالي لم يتجاوز 79%."""
    df = state.df.iloc[-3:]
    close = float(state.df["close"].iloc[-1])
    if d == "bull":      # الارتداد نزولًا من قمة الساق
        depth = (leg["hi"] - float(df["low"].min())) / leg["size"]
        now = (leg["hi"] - close) / leg["size"]
    else:                # الارتداد صعودًا من قاع الساق الهابطة
        depth = (float(df["high"].max()) - leg["lo"]) / leg["size"]
        now = (close - leg["lo"]) / leg["size"]
    return OTE_LO - 0.02 <= depth <= 0.85 and now <= OTE_HI + 0.02


class SmcOteFvg(Strategy):
    key, label = "smc_ote_fvg", "SMC OTE + FVG"

    def _setup(self, ctx, tf: str, d: str):
        st = ctx.tf[tf]
        leg = impulse_leg(st, d)
        if leg is None:
            return None
        top, bot = ote_band(leg, d)
        zones = list(st.zones) + (list(ctx.tf["5m"].zones) if tf != "5m" else [])
        fvg = [z for z in zones if z.kind.startswith("fvg") and z.direction == d and z.ts >= leg["start_ts"]
               and _overlap(z.top, z.bottom, top, bot)]
        ob = [z for z in zones if z.kind in ("demand", "supply") and z.direction == d and z.impulse >= 1.5
              and _overlap(z.top, z.bottom, top, bot)]
        return {"tf": tf, "leg": leg, "fvg": fvg, "ob": ob, "in_ote": in_ote(st, leg, d)}

    def check(self, ctx, d):
        s5, s15 = ctx.tf["5m"], ctx.tf["15m"]
        setup = None
        for tf in ("5m", "15m"):
            cand = self._setup(ctx, tf, d)
            if cand is not None and (setup is None or (cand["in_ote"] and not setup["in_ote"])):
                setup = cand
            if setup is not None and setup["in_ote"]:
                break
        ch = Checks()
        ch.add("BOS/CHoCH in direction", bool(s5.recent_break(d, 40) or s15.recent_break(d, 20)), 15, True)
        ch.add("impulse leg", setup is not None, 10, True)
        ch.add("price in OTE 62-79%", bool(setup and setup["in_ote"]), 20, True)
        ch.add("confirmation candle", _confirm_candle(s5.df, d), 15, True)
        ch.add("HTF not opposing", _htf_ok(ctx, d), 10, True)
        ch.add("FVG overlaps OTE", bool(setup and setup["fvg"]), 20, True)
        ch.add("strict order block at OTE", bool(setup and setup["ob"]), 10)
        ch.add("liquidity sweep (SMC)", any(s.expected == d for s in s5.sweeps + s15.sweeps), 10)
        l, prev = s5.last, s5.df["macd_hist"].iloc[-2]
        up = float(l["macd_hist"]) > float(prev)
        rsi = float(l["rsi"])
        ch.add("momentum (SMC)", (up and 35 <= rsi <= 70) if d == "bull" else ((not up) and 30 <= rsi <= 65), 10)
        inval = None
        if setup:
            inval = setup["leg"]["lo"] if d == "bull" else setup["leg"]["hi"]
        return ch, inval, {"setup": "SMC OTE + FVG", "tf": setup["tf"] if setup else None}


class SmcSweepMssFvg(Strategy):
    key, label = "smc_sweep_mss_fvg", "SMC Sweep-MSS-FVG"

    def check(self, ctx, d):
        s5, s15 = ctx.tf["5m"], ctx.tf["15m"]
        df = s5.df
        a = max(s5.atr, 1e-9)
        sweeps = sorted([s for s in s5.sweeps if s.expected == d], key=lambda s: s.bars_ago)
        sw = sweeps[0] if sweeps else None
        ch = Checks()
        ch.add("liquidity sweep", sw is not None, 20, True)
        mss = None
        if sw is not None:
            for b in reversed(s5.breaks):
                if b.direction == d and b.idx >= sw.idx:
                    mss = b
                    break
        ch.add("MSS after sweep", mss is not None, 20, True)
        zone = None
        if sw is not None and mss is not None:
            t0 = df.index[max(sw.idx - 1, 0)]
            zs = [z for z in s5.zones if z.kind.startswith("fvg") and z.direction == d and z.ts >= t0]
            zone = max(zs, key=lambda z: z.score) if zs else None
        ch.add("FVG created by MSS", zone is not None, 20, True)
        inside = False
        if zone is not None:
            close = float(df["close"].iloc[-1])
            if d == "bull":
                inside = float(df["low"].iloc[-3:].min()) <= zone.top and close > zone.bottom
            else:
                inside = float(df["high"].iloc[-3:].max()) >= zone.bottom and close < zone.top
        ch.add("price inside FVG", inside, 20, True)
        ch.add("HTF not opposing", _htf_ok(ctx, d), 10, True)
        ch.add("sweep confirmed", bool(sw and sw.confirmed), 10)
        ch.add("pool quality", bool(sw and sw.pool_kind != "swing"), 10)
        ch.add("session active", ctx.session["label"] != "Off-hours", 10)
        ch.add("confirmation candle", _confirm_candle(df, d), 10)
        return ch, (sw.extreme if sw else None), {"setup": "SMC Sweep-MSS-FVG"}

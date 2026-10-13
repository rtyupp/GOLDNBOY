"""استراتيجيتان متقدمتان مبنيّتان على أدوات المشروع نفسها (بلا تكرار منطق) وبتعريفات
مأخوذة من الأدبيات العامة (ICT killzones / Silver Bullet، ومستويات اليوم السابق PDH/PDL):

1) LiquidityGrab  — سحب قمة/قاع اليوم السابق (PDH/PDL) ثم استرجاع المستوى: أقوى مستويات السيولة
   اليومية، لأن اليوم السابق يجمع أعلى تركيز أوامر الإيقاف. يختلف عن LiquiditySweep العام لأنه
   يشترط أن يكون المستوى المسحوب هو حد اليوم السابق تحديدًا.
2) SmcSilverBullet — نافذة ICT الزمنية (Silver Bullet / killzone) + OTE + FVG + MSS: التوقيت
   أحد أقوى عوامل الذهب (سيولة لندن/نيويورك)، ولم يكن هناك أي مراعاة زمنية في الاستراتيجيات القائمة.

كل الشروط سببية: شموع مغلقة فقط، ومستويات اليوم السابق المكتمل، ونوافذ زمنية معروفة مسبقًا.
"""
from __future__ import annotations

from typing import Optional

from goldbot.strategies.base import Strategy, Checks, htf_required
from goldbot.strategies.smc import impulse_leg, ote_band, in_ote, _confirm_candle, _overlap


class LiquidityGrab(Strategy):
    key, label = "liquidity_grab", "Liquidity Grab (PDH/PDL)"

    def check(self, ctx, d):
        ch = Checks()
        want = "PDH" if d == "bear" else "PDL"
        s5, s15 = ctx.tf["5m"], ctx.tf["15m"]
        cands = [s for s in s5.sweeps + s15.sweeps
                 if s.expected == d and s.pool_kind == want and s.bars_ago <= 16]
        cands.sort(key=lambda s: (not s.confirmed, s.bars_ago))
        sw = cands[0] if cands else None
        lvl = sw.level if sw else None
        l = s5.last
        close = float(l["close"])
        reclaimed = bool(sw is not None and ((close < lvl) if d == "bear" else (close > lvl)))
        ch.add("daily level sweep (PDH/PDL)", sw is not None, 30, True)
        ch.add("level reclaimed", reclaimed, 20, True)
        ch.add("confirmation candle", _confirm_candle(s5.df, d), 20, True)
        ch.add("rejection / stop-hunt", bool(sw and sw.stop_hunt), 15)
        ch.add("HTF not opposing", ctx.mtf["bias"] in (d, "neutral"), 15, htf_required(self.cfg))
        ch.add("session active", ctx.session["label"] in ("London", "New York", "London/New York"), 10)
        mom = ((float(l["rsi"]) > 50) == (d == "bull")) and ((float(l["macd_hist"]) > 0) == (d == "bull"))
        ch.add("momentum", mom, 10)
        inval = sw.extreme if sw else None
        return ch, inval, {"setup": "Liquidity Grab (PDH/PDL)", "level": round(lvl, 2) if lvl is not None else None}


class SmcSilverBullet(Strategy):
    key, label = "silver_bullet", "Silver Bullet (Killzone OTE+FVG)"

    def _in_window(self, ts) -> bool:
        wins = self.cfg.get("strategies.silver_bullet.windows", [[7, 8], [14, 15], [18, 19]])
        m = ts.hour + ts.minute / 60.0
        return any(a <= m < b for a, b in wins)

    def check(self, ctx, d):
        ch = Checks()
        ch.add("killzone window", self._in_window(ctx.ts), 25, True)
        s5, s15 = ctx.tf["5m"], ctx.tf["15m"]
        setup: Optional[dict] = None
        for tf in ("5m", "15m"):
            st = ctx.tf[tf]
            leg = impulse_leg(st, d)
            if leg is None:
                continue
            top, bot = ote_band(leg, d)
            zones = list(st.zones) + (list(s5.zones) if tf != "5m" else [])
            fvg = [z for z in zones if z.kind.startswith("fvg") and z.direction == d and z.ts >= leg["start_ts"]
                   and _overlap(z.top, z.bottom, top, bot)]
            cand = {"tf": tf, "leg": leg, "fvg": fvg, "in_ote": in_ote(st, leg, d)}
            if setup is None or (cand["in_ote"] and not setup["in_ote"]):
                setup = cand
            if setup["in_ote"]:
                break
        ch.add("impulse leg", setup is not None, 10, True)
        ch.add("price in OTE 62-79%", bool(setup and setup["in_ote"]), 20, True)
        ch.add("FVG overlaps OTE", bool(setup and setup["fvg"]), 20, True)
        ch.add("BOS/CHoCH in direction", bool(s5.recent_break(d, 30) or s15.recent_break(d, 15)), 15, True)
        ch.add("confirmation candle", _confirm_candle(s5.df, d), 10, True)
        ch.add("HTF not opposing", ctx.mtf["bias"] in (d, "neutral"), 10, htf_required(self.cfg))
        ch.add("displacement (ATR)", bool(setup and setup["leg"]["size"] >= 1.5 * ctx.tf["15m"].atr), 10)
        inval = (setup["leg"]["lo"] if d == "bull" else setup["leg"]["hi"]) if setup else None
        return ch, inval, {"setup": "Silver Bullet (Killzone OTE+FVG)", "tf": setup["tf"] if setup else None}

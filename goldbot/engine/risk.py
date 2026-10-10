"""Risk engine: SL from market structure + ATR buffer (not a fixed %), TP from RR + liquidity targets."""
from __future__ import annotations
from typing import Optional, Tuple
from goldbot.models import RiskPlan


class RiskEngine:
    def __init__(self, cfg):
        r = cfg.section("risk")
        self.sl_buffer_atr = r.get("sl_buffer_atr", 0.25)
        self.min_sl_atr = r.get("min_sl_atr", 0.8)
        self.max_sl_atr = r.get("max_sl_atr", 3.0)
        self.rr1 = r.get("rr1", 1.2)
        self.rr2 = r.get("rr2", 2.4)
        self.min_rr2 = r.get("min_rr2", 1.8)
        self.max_spread = r.get("max_spread", 0.8)
        self.max_spread_to_risk = r.get("max_spread_to_risk", 0.12)
        self.tp_buffer_atr = r.get("tp_buffer_atr", 0.1)

    def _targets(self, ctx, direction: str, entry: float):
        prices = []
        for tf in ("15m", "1H"):
            for p in ctx.tf[tf].pools:
                if direction == "bull" and p.side == "buy" and p.price > entry:
                    prices.append(p.price)
                if direction == "bear" and p.side == "sell" and p.price < entry:
                    prices.append(p.price)
        for name, v in ctx.levels.items():
            if direction == "bull" and v > entry and name.endswith("H"):
                prices.append(v)
            if direction == "bear" and v < entry and name.endswith("L"):
                prices.append(v)
        return sorted(set(prices), reverse=(direction == "bear"))

    def build(self, ctx, direction: str, invalidation: float) -> Tuple[Optional[RiskPlan], Optional[str]]:
        bull = direction == "bull"
        atr = ctx.atr
        if ctx.spread is None:
            return None, "NO_QUOTE"
        entry = ctx.ask if bull else ctx.bid
        notes = []
        sl = invalidation - self.sl_buffer_atr * atr if bull else invalidation + self.sl_buffer_atr * atr
        if (bull and sl >= entry) or ((not bull) and sl <= entry):
            return None, "SL_ILLOGICAL"
        risk = abs(entry - sl)
        if risk < self.min_sl_atr * atr:
            sl = entry - self.min_sl_atr * atr if bull else entry + self.min_sl_atr * atr
            risk = abs(entry - sl)
            notes.append("تم توسيع وقف الخسارة إلى الحد الأدنى بحسب ATR")
        if risk > self.max_sl_atr * atr:
            return None, f"SL_TOO_WIDE({risk / atr:.1f}ATR>{self.max_sl_atr}ATR)"
        if ctx.spread > self.max_spread:
            return None, f"SPREAD_HIGH({ctx.spread:.2f}>{self.max_spread})"
        if ctx.spread > self.max_spread_to_risk * risk:
            return None, f"SPREAD_VS_RISK({ctx.spread:.2f}/{risk:.2f})"
        sgn = 1 if bull else -1
        tp1, tp2 = entry + sgn * self.rr1 * risk, entry + sgn * self.rr2 * risk
        targets = self._targets(ctx, direction, entry)
        if targets:
            d0 = abs(targets[0] - entry)
            if d0 < 1.0 * risk:
                return None, f"RR_POOR_TARGET({d0 / risk:.1f}R)"
            if d0 < abs(tp1 - entry) + self.tp_buffer_atr * atr:
                tp1 = targets[0] - sgn * self.tp_buffer_atr * atr
                notes.append("الهدف الأول وُضع قبل أقرب سيولة")
            for t in targets[1:]:
                dist = abs(t - entry)
                if abs(tp1 - entry) + 0.3 * risk <= dist < abs(tp2 - entry):
                    tp2 = t - sgn * self.tp_buffer_atr * atr
                    notes.append("الهدف الثاني وُضع قبل السيولة التالية")
                    break
        rr1, rr2 = abs(tp1 - entry) / risk, abs(tp2 - entry) / risk
        if rr2 < self.min_rr2:
            return None, f"RR_POOR(RR2={rr2:.2f}<{self.min_rr2})"
        return RiskPlan("BUY" if bull else "SELL", round(entry, 2), round(sl, 2), round(tp1, 2), round(tp2, 2),
                        round(risk, 2), round(abs(tp1 - entry), 2), round(abs(tp2 - entry), 2),
                        round(rr1, 2), round(rr2, 2), notes), None

    @staticmethod
    def risk_level(ctx, plan: RiskPlan) -> str:
        atr = ctx.atr
        pts = 0
        pts += 2 if plan.risk > 2.2 * atr else (1 if plan.risk > 1.5 * atr else 0)
        pts += 2 if (ctx.spread or 0) > 0.08 * plan.risk else 0
        pts += 1 if ctx.mtf["bias"] in ("conflict", "neutral") else 0
        return "High" if pts >= 3 else ("Medium" if pts >= 1 else "Low")

"""اختبارات الإضافات المتقدمة: استراتيجيتا LiquidityGrab وSilverBullet + بوابة حالة السوق. كلها بلا شبكة."""
import unittest
from types import SimpleNamespace as NS

import pandas as pd

from goldbot.engine import no_trade
from goldbot.engine.risk import RiskEngine
from goldbot.strategies.advanced import LiquidityGrab, SmcSilverBullet
from goldbot.strategies.impl import ALL, LiquiditySweep
from tests.helpers import cfg


def _df(closes, highs=None, lows=None):
    n = len(closes)
    idx = pd.date_range("2026-10-06 10:00", periods=n, freq="5min", tz="UTC")
    opens = [closes[0]] + list(closes[:-1])
    highs = highs or [max(o, c) + 0.1 for o, c in zip(opens, closes)]
    lows = lows or [min(o, c) - 0.1 for o, c in zip(opens, closes)]
    return pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes}, index=idx)


def _ctx(closes, *, sweeps5=(), sweeps15=(), bias="bull", session="London", ts=None, breaks=None):
    last = {"close": closes[-1], "rsi": 60.0, "macd_hist": 0.5}

    def recent_break(direction, bars):
        return None

    def state(sweeps):
        return NS(sweeps=list(sweeps), df=_df(closes), last=last, swings=[], zones=[],
                  recent_break=recent_break, breaks=list(breaks or []), atr=2.0)

    st5, st15 = state(sweeps5), state(sweeps15)
    return NS(tf={"5m": st5, "15m": st15, "1m": st5}, mtf={"bias": bias},
              session={"label": session}, ts=ts or pd.Timestamp("2026-10-06 10:05", tz="UTC"),
              mid=closes[-1])


def _sweep(pool, expected, extreme, level, bars_ago=1, confirmed=True, stop_hunt=True):
    return NS(pool_kind=pool, expected=expected, extreme=extreme, level=level, bars_ago=bars_ago,
              confirmed=confirmed, stop_hunt=stop_hunt)


class TestLiquidityGrab(unittest.TestCase):
    def setUp(self):
        self.c = cfg()
        self.s = LiquidityGrab(self.c, RiskEngine(self.c))

    def test_pdh_sweep_reclaim_is_sell(self):
        # قمة اليوم السابق سُحبت ثم أُغلق تحتها ⇒ إعداد بيع (سحب سيولة ثم انعكاس)
        closes = [100.9, 100.8, 100.6, 100.5, 100.3]
        ctx = _ctx(closes, sweeps5=[_sweep("PDH", "bear", 100.9, 100.5)], bias="bear")
        ch, inval, tags = self.s.check(ctx, "bear")
        self.assertTrue(ch.required_ok)
        self.assertEqual(inval, 100.9)
        self.assertEqual(tags["level"], 100.5)
        self.assertGreaterEqual(ch.score, self.c.get("strategies.min_score", 60))

    def test_other_pool_does_not_trigger(self):
        closes = [100.9, 100.8, 100.6, 100.5, 100.3]
        ctx = _ctx(closes, sweeps5=[_sweep("swing", "bear", 100.9, 100.5)], bias="bear")   # ليست PDH/PDL
        ch, _, _ = self.s.check(ctx, "bear")
        self.assertFalse(ch.required_ok)

    def test_without_reclaim_required_fails(self):
        closes = [100.9, 100.8, 100.6, 100.5, 100.6]   # الإغلاق فوق المستوى المسحوب (100.5)
        ctx = _ctx(closes, sweeps5=[_sweep("PDH", "bear", 100.9, 100.5)], bias="bear")
        ch, _, _ = self.s.check(ctx, "bear")
        self.assertFalse(ch.required_ok)

    def test_pdl_sweep_reclaim_is_buy(self):
        closes = [99.1, 99.3, 99.4, 99.5, 99.7]
        ctx = _ctx(closes, sweeps5=[_sweep("PDL", "bull", 99.1, 99.5)], bias="bull")
        ch, inval, _ = self.s.check(ctx, "bull")
        self.assertTrue(ch.required_ok)
        self.assertEqual(inval, 99.1)


class TestSilverBullet(unittest.TestCase):
    def setUp(self):
        self.c = cfg()
        self.s = SmcSilverBullet(self.c, RiskEngine(self.c))

    def test_killzone_windows(self):
        for h in (7, 14, 18):
            self.assertTrue(self.s._in_window(pd.Timestamp(f"2026-10-06 {h:02d}:30", tz="UTC")), h)
        for h in (3, 10, 13, 22):
            self.assertFalse(self.s._in_window(pd.Timestamp(f"2026-10-06 {h:02d}:30", tz="UTC")), h)

    def test_window_is_required(self):
        closes = [100.0, 100.2, 100.1, 100.4, 100.6]
        ctx = _ctx(closes, ts=pd.Timestamp("2026-10-06 10:05", tz="UTC"))   # خارج النوافذ
        ch, _, _ = self.s.check(ctx, "bull")
        self.assertFalse(ch.required_ok)

    def test_full_bull_setup_fires(self):
        # ساق صاعدة 100→110، ارتداد إلى OTE (102.1–103.8)، فجوة FVG داخل OTE، داخل نافذة 14:00
        closes = [100, 101, 105, 108, 110, 109, 107, 105, 104, 103.2, 103.8]
        ts = pd.Timestamp("2026-10-06 14:05", tz="UTC")
        idx = pd.date_range("2026-10-06 14:00", periods=len(closes), freq="5min", tz="UTC")
        swings = [NS(kind="L", idx=0, price=100.0), NS(kind="H", idx=4, price=110.0)]
        zone = NS(kind="fvg_bull", direction="bull", top=104.0, bottom=102.0, ts=idx[5], score=1.0)
        opens = [closes[0]] + list(closes[:-1])
        df = pd.DataFrame({"open": opens, "high": [max(o, c) + 0.1 for o, c in zip(opens, closes)],
                           "low": [min(o, c) - 0.1 for o, c in zip(opens, closes)], "close": closes}, index=idx)
        st = NS(sweeps=[], df=df, last={"close": closes[-1], "rsi": 60.0, "macd_hist": 0.5},
                swings=swings, zones=[zone], recent_break=lambda d, b: NS(), atr=2.0)
        ctx = NS(tf={"5m": st, "15m": st}, mtf={"bias": "bull"}, session={"label": "New York"}, ts=ts)
        ch, inval, tags = self.s.check(ctx, "bull")
        self.assertTrue(ch.required_ok)
        self.assertTrue(ch.as_dict()["price in OTE 62-79%"])
        self.assertTrue(ch.as_dict()["FVG overlaps OTE"])
        self.assertEqual(inval, 100.0)
        self.assertEqual(tags["tf"], "5m")
        self.assertGreaterEqual(ch.score, self.c.get("strategies.min_score", 60))


class TestStaleSweepGuard(unittest.TestCase):
    """Regression: a sweep whose invalidation extreme is already violated by price used to score
    high yet be rejected by risk (SL_ILLOGICAL). LiquiditySweep must not report such stale setups."""

    def setUp(self):
        self.c = cfg()
        self.s = LiquiditySweep(self.c, RiskEngine(self.c))

    def test_stale_bull_sweep_does_not_fire(self):
        # سحب قاع 100.0، ثم هبط السعر تحته (99.5) ⇒ الإبطال (100.0) صار أعلى من السعر ⇒ إعداد بائد
        closes = [100.6, 100.4, 100.2, 100.0, 99.5]
        ctx = _ctx(closes, sweeps5=[_sweep("swing", "bull", 100.0, 99.6)], bias="bull")
        ch, _, _ = self.s.check(ctx, "bull")
        self.assertFalse(ch.required_ok)
        self.assertFalse(ch.as_dict()["price still beyond sweep invalidation"])

    def test_fresh_bull_sweep_can_fire(self):
        # نفس السحب لكن السعر ما زال فوق قمة الشمعة ⇒ الإبطال سليم
        closes = [99.0, 99.4, 99.8, 100.0, 99.9]
        ctx = _ctx(closes, sweeps5=[_sweep("swing", "bull", 99.85, 99.5)], bias="bull")
        ch, _, _ = self.s.check(ctx, "bull")
        self.assertTrue(ch.as_dict()["price still beyond sweep invalidation"])


class TestRegimeGuard(unittest.TestCase):
    def _ctx(self, regime, spread=0.2, bias="neutral", er=0.5):
        s15 = NS(last={"er": er}, trend="RANGE")
        s1h = NS(trend="RANGE")
        return NS(spread=spread, mtf={"bias": bias}, tf={"15m": s15, "1H": s1h}, regime=regime,
                  session={"label": "London"})

    def test_default_blocks_transition(self):
        # الافتراضي الآن يحجب النظام الانتقالي (أكثر الأنظمة خسارةً على بيانات 28 يومًا).
        c = cfg()
        reasons = no_trade.global_gates(c, self._ctx("TRANSITION"), True, "OK")
        self.assertTrue(any(r.startswith("REGIME_BLOCK") for r in reasons))

    def test_no_block_when_disabled(self):
        c = cfg(filters__block_regimes=[])
        self.assertEqual(no_trade.global_gates(c, self._ctx("TRANSITION"), True, "OK"), [])

    def test_block_configured_regime(self):
        c = cfg(filters__block_regimes=["TRANSITION"])
        reasons = no_trade.global_gates(c, self._ctx("TRANSITION"), True, "OK")
        self.assertTrue(any(r.startswith("REGIME_BLOCK") for r in reasons))
        self.assertEqual(no_trade.global_gates(c, self._ctx("BULLISH"), True, "OK"), [])


class TestRegistry(unittest.TestCase):
    def test_both_advanced_strategies_registered(self):
        keys = [c.key for c in ALL]
        self.assertIn("liquidity_grab", keys)
        self.assertIn("silver_bullet", keys)
        self.assertEqual(len(keys), len(set(keys)), "لا مفاتيح مكررة")

    def test_all_enabled_in_default_config(self):
        c = cfg()
        enabled = set(c.get("strategies.enabled"))
        self.assertTrue({"liquidity_grab", "silver_bullet"} <= enabled)


if __name__ == "__main__":
    unittest.main()

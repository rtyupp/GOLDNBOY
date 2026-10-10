import unittest
from types import SimpleNamespace as NS
import pandas as pd

from goldbot.strategies import smc
from goldbot.strategies.impl import ALL
from goldbot.analysis.analyzer import Analyzer, ANALYZED
from goldbot.providers.synthetic import make_synthetic_1m
from goldbot.core.market_data import MarketData
from tests.helpers import cfg


def state(swings, closes, lows=None, highs=None, atr=2.0):
    n = len(closes)
    idx = pd.date_range("2026-01-05 10:00", periods=n, freq="5min", tz="UTC")
    df = pd.DataFrame({"open": closes, "high": highs or closes, "low": lows or closes, "close": closes}, index=idx)
    return NS(swings=[NS(kind=k, idx=i, price=p) for k, i, p in swings], df=df, atr=atr)


class TestLeg(unittest.TestCase):
    def test_bull_leg_and_ote_band(self):
        closes = [100] * 5 + [110] * 10
        st = state([("L", 4, 100.0), ("H", 9, 110.0)], closes)
        leg = smc.impulse_leg(st, "bull")
        self.assertEqual((leg["lo"], leg["hi"], leg["size"]), (100.0, 110.0, 10.0))
        top, bot = smc.ote_band(leg, "bull")
        self.assertAlmostEqual(top, 110 - 0.62 * 10)
        self.assertAlmostEqual(bot, 110 - 0.79 * 10)

    def test_bear_leg_and_ote_band(self):
        closes = [110] * 5 + [100] * 10
        st = state([("H", 4, 110.0), ("L", 9, 100.0)], closes)
        leg = smc.impulse_leg(st, "bear")
        top, bot = smc.ote_band(leg, "bear")
        self.assertAlmostEqual(top, 100 + 0.79 * 10)
        self.assertAlmostEqual(bot, 100 + 0.62 * 10)

    def test_leg_too_small_or_broken_rejected(self):
        small = state([("L", 4, 100.0), ("H", 9, 101.0)], [100] * 15)
        self.assertIsNone(smc.impulse_leg(small, "bull"))
        broken = state([("L", 4, 100.0), ("H", 9, 110.0)], [100] * 5 + [110] * 5 + [99] * 5)
        self.assertIsNone(smc.impulse_leg(broken, "bull"))

    def test_leg_wrong_order_rejected(self):
        st = state([("H", 4, 110.0), ("L", 9, 100.0)], [105] * 15)
        self.assertIsNone(smc.impulse_leg(st, "bull"))

    def test_in_ote_bull(self):
        closes = [100] * 5 + [110] * 7 + [104, 103.5, 103.4]
        lows = list(closes); lows[-1] = 102.5
        st = state([("L", 4, 100.0), ("H", 9, 110.0)], closes, lows=lows)
        leg = smc.impulse_leg(st, "bull")
        self.assertTrue(smc.in_ote(st, leg, "bull"))
        shallow = state([("L", 4, 100.0), ("H", 9, 110.0)], [100] * 5 + [110] * 7 + [108, 108, 108])
        self.assertFalse(smc.in_ote(shallow, smc.impulse_leg(shallow, "bull"), "bull"))

    def test_in_ote_bear(self):
        closes = [110] * 5 + [100] * 7 + [106, 106.3, 106.5]
        highs = list(closes); highs[-1] = 107.2
        st = state([("H", 4, 110.0), ("L", 9, 100.0)], closes, highs=highs)
        self.assertTrue(smc.in_ote(st, smc.impulse_leg(st, "bear"), "bear"))


class TestStrategiesRun(unittest.TestCase):
    def test_registered_and_enabled(self):
        keys = [c.key for c in ALL]
        self.assertIn("smc_ote_fvg", keys)
        self.assertIn("smc_sweep_mss_fvg", keys)
        c = cfg()
        from goldbot.config import load_config
        enabled = load_config("config/config.yaml").get("strategies.enabled")
        self.assertIn("smc_ote_fvg", enabled)
        self.assertIn("smc_sweep_mss_fvg", enabled)

    def test_evaluate_never_crashes_and_plans_are_valid(self):
        c = cfg()
        df = make_synthetic_1m(days=20, seed=3)
        md = MarketData()
        md.history_rows = len(df)
        md.load_history(df)
        an = Analyzer(c)
        strat = [k(c, __import__("goldbot.engine.risk", fromlist=["RiskEngine"]).RiskEngine(c)) for k in ALL if k.key.startswith("smc")]
        fired, checked = 0, 0
        for t in pd.date_range(df.index[-1] - pd.Timedelta(days=4), df.index[-1], freq="15min"):
            frames = md.frames(t)
            px = float(frames["1m"]["close"].iloc[-1])
            ctx, why = an.analyze(frames, px - 0.15, px + 0.15, t)
            if ctx is None:
                continue
            for s in strat:
                r = s.evaluate(ctx)
                checked += 1
                self.assertIn(r.signal, ("BUY", "SELL", "NONE"))
                if r.signal != "NONE":
                    fired += 1
                    p = r.plan
                    self.assertTrue(p.sl < p.entry < p.tp1 < p.tp2 if p.direction == "BUY" else p.sl > p.entry > p.tp1 > p.tp2)
        self.assertGreater(checked, 50)


if __name__ == "__main__":
    unittest.main()

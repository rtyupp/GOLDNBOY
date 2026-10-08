import unittest
import numpy as np, pandas as pd
from tests.helpers import ohlc, series_from_closes, cfg
from goldbot.analysis import structure as st, liquidity as lq
from goldbot.analysis.indicators import add_indicators, _rsi
from goldbot.providers.synthetic import make_synthetic_1m
from goldbot.core.market_data import MarketData
from goldbot.analysis.analyzer import Analyzer
from goldbot.analysis.news import NewsFilter, NewsStatus


def zigzag(points, per=5):
    closes = []
    for a, b in zip(points[:-1], points[1:]):
        closes += list(np.linspace(a, b, per, endpoint=False))
    closes.append(points[-1])
    return series_from_closes(closes)


class TestStructure(unittest.TestCase):
    def test_bullish_hh_hl_and_bos(self):
        df = zigzag([100, 110, 104, 118, 111, 126, 119, 134])
        sw = st.find_swings(df, 2, 2)
        res = st.analyze_structure(df, atr=2.0, left=2, right=2)
        self.assertEqual(res["trend"], "BULLISH")
        self.assertEqual(res["labels"], ("HH", "HL"))
        self.assertTrue(any(b.kind == "BOS" and b.direction == "bull" for b in res["breaks"]))

    def test_bearish(self):
        df = zigzag([140, 130, 136, 120, 126, 110, 116, 100])
        res = st.analyze_structure(df, atr=2.0, left=2, right=2)
        self.assertEqual(res["trend"], "BEARISH")

    def test_choch_after_bullish_trend(self):
        df = zigzag([100, 110, 104, 118, 111, 126, 119, 134, 118, 100, 108, 90])
        res = st.analyze_structure(df, atr=2.0, left=2, right=2)
        kinds = [(b.kind, b.direction) for b in res["breaks"]]
        self.assertIn(("CHoCH", "bear"), kinds)

    def test_swings_are_confirmed_only(self):
        df = zigzag([100, 110, 104, 118, 111, 126])
        for s in st.find_swings(df, 3, 3):
            self.assertLessEqual(s.confirmed_idx, len(df) - 1)
            self.assertEqual(s.confirmed_idx, s.idx + 3)

    def test_no_repaint_prefix_stability(self):
        """Swings/breaks computed on a prefix equal those from the longer series (up to confirmation)."""
        df = make_synthetic_1m(days=3, seed=3).resample("5min").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
        n = len(df)
        a = st.find_swings(df.iloc[:n - 40], 3, 3)
        b = [s for s in st.find_swings(df, 3, 3) if s.confirmed_idx <= n - 41]
        self.assertEqual([(s.idx, s.kind, round(s.price, 4)) for s in a][:-2], [(s.idx, s.kind, round(s.price, 4)) for s in b][:len(a) - 2])


class TestLiquidity(unittest.TestCase):
    def mk(self, extra_bars):
        base = [100, 101, 102, 103, 104, 105, 104, 103, 102, 103, 104, 105.02, 104, 103.5]
        df = series_from_closes(base + extra_bars, spread=0.1)
        df["atr"] = 1.0
        return df

    def test_buy_side_sweep_then_confirmation(self):
        df = self.mk([])
        # prior swing high ~105.1; sweep bar: wicks to 106.5 and closes back at 104.2, next closes lower than sweep low
        rows = df[["open", "high", "low", "close"]].values.tolist()
        rows += [(104.0, 106.5, 103.9, 104.2), (104.2, 104.3, 103.0, 103.2)]
        d = ohlc(rows)
        d["atr"] = 1.0
        swings = st.find_swings(d, 2, 2)
        pools = lq.build_pools(swings, 1.0, {})
        sweeps = lq.detect_sweeps(d, pools, 1.0, lookback=4)
        bear = [s for s in sweeps if s.expected == "bear"]
        self.assertTrue(bear)
        self.assertTrue(any(s.confirmed for s in bear))
        self.assertGreaterEqual(bear[0].extreme, 106.5)

    def test_plain_breakout_is_not_a_sweep(self):
        df = series_from_closes([100, 101, 102, 103, 104, 105, 104, 103, 102, 103, 104, 105, 106, 107, 108], spread=0.1)
        df["atr"] = 1.0
        swings = st.find_swings(df, 2, 2)
        sweeps = lq.detect_sweeps(df, lq.build_pools(swings, 1.0, {}), 1.0, lookback=4)
        self.assertEqual([s for s in sweeps if s.kind == "sweep"], [])

    def test_equal_highs_detected(self):
        df = zigzag([100, 110, 104, 110.05, 104, 109, 103])
        swings = st.find_swings(df, 2, 2)
        pools = lq.build_pools(swings, 2.0, {})
        self.assertTrue(any(p.kind == "equal" and p.side == "buy" for p in pools))


class TestIndicators(unittest.TestCase):
    def test_short_startup_frame_uses_safe_internal_indicators(self):
        df = ohlc([(1, 2, 0.5, 1.5)] * 20, freq="1min")
        out = add_indicators(df, use_ta=True)
        self.assertEqual(len(out), 20)
        self.assertIn("atr", out.columns)

    def test_rsi_extremes_and_columns(self):
        up = pd.Series(np.arange(1, 80, dtype=float))
        self.assertGreater(_rsi(up).iloc[-1], 99)
        df = make_synthetic_1m(days=5).resample("5min").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
        d = add_indicators(df, use_ta=False)
        for c in ["ema_9", "ema_20", "ema_50", "ema_100", "ema_200", "sma_200", "rsi", "macd", "macd_signal", "stoch_k", "roc", "atr", "bb_high", "bb_low", "std", "vol", "rel_vol", "vwap"]:
            self.assertIn(c, d.columns)
        self.assertTrue(((d["rsi"].dropna() >= 0) & (d["rsi"].dropna() <= 100)).all())

    def test_indicators_are_causal(self):
        df = make_synthetic_1m(days=4).resample("5min").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
        full = add_indicators(df, False)
        cut = add_indicators(df.iloc[:600], False)
        for c in ["ema_50", "rsi", "atr", "macd", "vwap", "bb_high"]:
            np.testing.assert_allclose(full[c].iloc[:600].values, cut[c].values, rtol=1e-9, equal_nan=True)


class TestNoLookahead(unittest.TestCase):
    def test_analysis_at_t_ignores_future(self):
        c = cfg()
        df = make_synthetic_1m(days=25, seed=5)
        t = df.index[-3000]
        as_of = t + pd.Timedelta(minutes=1)
        md_full = MarketData(); md_full.load_history(df)
        md_cut = MarketData(); md_cut.load_history(df[df.index <= t])
        a1, a2 = Analyzer(c), Analyzer(c)
        px = float(df.loc[t, "close"])
        x1, w1 = a1.analyze(md_full.frames(as_of), px, px + .3, as_of, NewsStatus("CLEAR"))
        x2, w2 = a2.analyze(md_cut.frames(as_of), px, px + .3, as_of, NewsStatus("CLEAR"))
        self.assertEqual((w1, w2), ("OK", "OK"))
        self.assertEqual(x1.mtf["labels"], x2.mtf["labels"])
        for tf in x1.tf:
            self.assertAlmostEqual(x1.tf[tf].atr, x2.tf[tf].atr, places=6)
            self.assertEqual(len(x1.tf[tf].sweeps), len(x2.tf[tf].sweeps))
        self.assertEqual({k: round(v, 4) for k, v in x1.levels.items()}, {k: round(v, 4) for k, v in x2.levels.items()})


class TestNews(unittest.TestCase):
    def test_windows(self):
        import tempfile, os
        p = os.path.join(tempfile.mkdtemp(), "e.csv")
        open(p, "w").write("datetime_utc,name,impact\n2026-06-05 12:30,NFP,high\n2026-06-05 18:00,Fed speech,medium\n")
        n = NewsFilter(p, 30, 15)
        T = lambda s: pd.Timestamp(s, tz="UTC")
        self.assertEqual(n.status(T("2026-06-05 11:50")).state, "CLEAR")
        self.assertEqual(n.status(T("2026-06-05 12:10")).state, "PRE_NEWS")
        self.assertEqual(n.status(T("2026-06-05 12:40")).state, "POST_NEWS")
        self.assertEqual(n.status(T("2026-06-05 12:50")).state, "CLEAR")
        self.assertEqual(n.status(T("2026-06-05 17:50")).state, "PRE_NEWS")    # keyword 'Fed' => treated as high impact


if __name__ == "__main__":
    unittest.main()

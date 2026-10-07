import os, tempfile, unittest
import numpy as np, pandas as pd
from tests.helpers import cfg
from goldbot.models import RiskPlan
from goldbot.backtest.engine import simulate_trade, metrics, run_backtest, store_probability_history, split_report
from goldbot.providers.synthetic import make_synthetic_1m
from goldbot.tracking.journal import Journal
from goldbot.ml.model import MLGate


def arr_from(rows, start="2026-06-01 10:00"):
    idx = pd.date_range(start, periods=len(rows), freq="1min", tz="UTC")
    a = np.array(rows, dtype=float)
    return {"index": idx, "high": a[:, 0], "low": a[:, 1], "close": a[:, 2]}


BUY = RiskPlan("BUY", 100.0, 95.0, 106.0, 112.0, 5.0, 6.0, 12.0, 1.2, 2.4)
T0 = pd.Timestamp("2026-06-01 10:00", tz="UTC")


class TestSim(unittest.TestCase):
    def test_sl_first_when_same_bar_touches_both(self):
        a = arr_from([(107, 94, 100)])               # touches TP1 and SL in one bar -> SL (conservative)
        self.assertEqual(simulate_trade(BUY, T0, a, 0.5, 0.0, 100)[:2], ("SL", -1.0))

    def test_tp1_then_be(self):
        a = arr_from([(107, 99, 106), (106, 99.5, 100)])
        res = simulate_trade(BUY, T0, a, 0.5, 0.0, 100)
        self.assertEqual(res[0], "TP1_BE")
        self.assertAlmostEqual(res[1], 0.6)

    def test_tp2(self):
        a = arr_from([(107, 99, 106), (113, 105, 112)])
        res = simulate_trade(BUY, T0, a, 0.5, 0.0, 100)
        self.assertEqual(res[0], "TP2")
        self.assertAlmostEqual(res[1], 0.5 * 1.2 + 0.5 * 2.4)

    def test_spread_makes_tp_harder(self):
        a = arr_from([(106.1, 99, 106)])             # high 106.1 - half-spread 0.5 < 106 => not hit
        self.assertEqual(simulate_trade(BUY, T0, a, 0.5, 1.0, 1)[0], "EXPIRED")

    def test_only_future_bars_used(self):
        a = arr_from([(200, 1, 100), (101, 99, 100)])      # bar BEFORE entry has crazy range
        res = simulate_trade(BUY, T0 + pd.Timedelta(minutes=1), a, 0.5, 0.0, 5)
        self.assertEqual(res[0], "EXPIRED")


class TestMetrics(unittest.TestCase):
    def test_metrics(self):
        df = pd.DataFrame({"r": [1.5, -1.0, 2.0, -1.0, -1.0], "outcome": ["TP2", "SL", "TP2", "SL", "SL"], "ts": range(5)})
        m = metrics(df)
        self.assertEqual((m["total_trades"], m["wins"], m["losses"]), (5, 2, 3))
        self.assertAlmostEqual(m["win_rate"], 0.4)
        self.assertAlmostEqual(m["profit_factor"], 3.5 / 3.0, places=2)
        self.assertAlmostEqual(m["max_drawdown_r"], 2.0)
        self.assertAlmostEqual(m["sl_pct"], 0.6)


class TestRun(unittest.TestCase):
    def test_smoke_and_store(self):
        c = cfg()
        df = make_synthetic_1m(days=16, seed=11)
        res = run_backtest(df, c, "5m", warmup_days=12, max_evals=120)
        self.assertIn("strategies", res)
        self.assertEqual(set(res["strategies"]), {"Trend Pullback", "Liquidity Sweep", "Breakout", "Reversal", "VWAP"})
        j = Journal(os.path.join(tempfile.mkdtemp(), "j.sqlite"))
        n = store_probability_history(j, res)
        self.assertEqual(len(j.history_trades()), n)


class TestML(unittest.TestCase):
    def test_refuses_without_data_and_has_no_leak_split(self):
        c = cfg(ml__enabled=True, ml__model_path=os.path.join(tempfile.mkdtemp(), "m.pkl"))
        g = MLGate(c)
        rep = g.train(pd.DataFrame({"features": [], "ts": [], "r": []}))
        self.assertFalse(rep["passed"])
        self.assertFalse(g.active)

    def test_random_labels_do_not_pass(self):
        import json
        rng = np.random.default_rng(1)
        n = 600
        rows = pd.DataFrame({"ts": pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC").astype(str),
                             "features": [json.dumps({"a": float(rng.normal()), "b": float(rng.normal())}) for _ in range(n)],
                             "r": rng.choice([-1.0, 1.5], n)})
        c = cfg(ml__enabled=True, ml__model_path=os.path.join(tempfile.mkdtemp(), "m.pkl"))
        rep = MLGate(c).train(rows)
        self.assertFalse(rep["passed"], rep)          # noise must not be activated
        self.assertLess(rep["train_end"], rep["test_start"])


if __name__ == "__main__":
    unittest.main()

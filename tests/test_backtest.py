import os, tempfile, unittest
import numpy as np, pandas as pd
from tests.helpers import cfg
from goldbot.models import RiskPlan
from goldbot.backtest.engine import simulate_trade, metrics, run_backtest, store_probability_history, walk_forward_folds, save_report
from goldbot.backtest.compare import verdict
from goldbot.providers.synthetic import make_synthetic_1m
from goldbot.tracking.journal import Journal


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


class TestSlippage(unittest.TestCase):
    """الانزلاق السعري: الدخول أسوأ، الوقف يُنفَّذ أسوأ، أهداف Limit بلا انزلاق إضافي."""

    def test_zero_slippage_matches_old_numbers(self):
        a = arr_from([(107, 99, 106), (113, 105, 112)])
        res = simulate_trade(BUY, T0, a, 0.5, 0.0, 100, 0.0, 0.0)
        self.assertEqual(res[0], "TP2")
        self.assertAlmostEqual(res[1], 0.5 * 1.2 + 0.5 * 2.4)

    def test_sl_costs_more_than_one_r(self):
        a = arr_from([(101, 94, 95)])
        r0 = simulate_trade(BUY, T0, a, 0.5, 0.0, 100)[1]
        r1 = simulate_trade(BUY, T0, a, 0.5, 0.0, 100, 0.5, 0.5)[1]
        self.assertEqual(r0, -1.0)
        self.assertAlmostEqual(r1, -(1 + 0.5 / 5 + 0.5 / 5))        # entry + stop slip، المخاطرة = 5

    def test_tp2_reduced_by_entry_slippage_only(self):
        a = arr_from([(107, 99, 106), (113, 105, 112)])
        r0 = simulate_trade(BUY, T0, a, 0.5, 0.0, 100)[1]
        r1 = simulate_trade(BUY, T0, a, 0.5, 0.0, 100, 0.5, 0.5)[1]
        self.assertAlmostEqual(r0 - r1, 0.5 / 5)                    # الدخول فقط (TP أمر Limit)

    def test_breakeven_exit_pays_stop_slippage(self):
        a = arr_from([(107, 99, 106), (106, 99.5, 100)])
        r0 = simulate_trade(BUY, T0, a, 0.5, 0.0, 100)[1]
        r1 = simulate_trade(BUY, T0, a, 0.5, 0.0, 100, 0.2, 0.3)[1]
        self.assertLess(r1, r0)

    def test_sell_direction_symmetric(self):
        sell = RiskPlan("SELL", 100.0, 105.0, 94.0, 88.0, 5.0, 6.0, 12.0, 1.2, 2.4)
        a = arr_from([(106, 99, 105)])
        self.assertAlmostEqual(simulate_trade(sell, T0, a, 0.5, 0.0, 100, 0.5, 0.5)[1], -1.2)


class TestMetrics(unittest.TestCase):
    def test_metrics(self):
        df = pd.DataFrame({"r": [1.5, -1.0, 2.0, -1.0, -1.0], "outcome": ["TP2", "SL", "TP2", "SL", "SL"], "ts": range(5)})
        m = metrics(df)
        self.assertEqual((m["total_trades"], m["wins"], m["losses"]), (5, 2, 3))
        self.assertAlmostEqual(m["win_rate"], 0.4)
        self.assertAlmostEqual(m["profit_factor"], 3.5 / 3.0, places=2)
        self.assertAlmostEqual(m["max_drawdown_r"], 2.0)
        self.assertAlmostEqual(m["sl_pct"], 0.6)


class TestWalkForward(unittest.TestCase):
    def _df(self, rs):
        return pd.DataFrame({"r": rs, "outcome": ["TP2" if r > 0 else "SL" for r in rs],
                             "ts": pd.date_range("2026-01-01", periods=len(rs), freq="1h", tz="UTC")})

    def test_too_small_returns_empty(self):
        self.assertEqual(walk_forward_folds(self._df([1.0, -1.0, 2.0]), n_folds=4, min_per_fold=5), {})

    def test_consistent_edge_across_folds(self):
        df = self._df([1.5, 1.0, -1.0] * 8)                 # كل طيّة موجبة
        wf = walk_forward_folds(df, n_folds=4)
        self.assertEqual(wf["n"], 4)
        self.assertTrue(wf["consistent"])
        self.assertEqual(wf["passed"], 4)

    def test_one_lucky_slice_fails_consistency(self):
        # الربح كله في الطيّة الأخيرة فقط: التقسيم 70/30 قد يخفي ذلك، والطيّات تكشفه
        rs = [-1.0] * 18 + [3.0] * 6
        wf = walk_forward_folds(self._df(rs), n_folds=4)
        self.assertFalse(wf["consistent"])
        self.assertLess(wf["passed"], 2)

    def test_verdict_requires_fold_consistency(self):
        rep = {"total_trades": 40, "avg_r": 0.3, "profit_factor": 1.5,
               "walk_forward_split": {"out_of_sample": {"avg_r": 0.2}},
               "walk_forward_folds": {"consistent": False}}
        self.assertIn("مرفوضة", verdict(rep))
        rep["walk_forward_folds"]["consistent"] = True
        self.assertIn("مقبولة", verdict(rep))

    def test_verdict_folds_optional_when_absent(self):
        rep = {"total_trades": 40, "avg_r": 0.3, "profit_factor": 1.5,
               "walk_forward_split": {"out_of_sample": {"avg_r": 0.2}}}
        self.assertIn("مقبولة", verdict(rep))               # لا طيّات (عينة صغيرة) => لا تُفرض

    def test_run_backtest_emits_folds(self):
        c = cfg()
        df = make_synthetic_1m(days=16, seed=11)
        res = run_backtest(df, c, "5m", warmup_days=12, max_evals=400)
        rep = res["combined"]
        self.assertIn("walk_forward_folds", rep)


class TestRun(unittest.TestCase):
    def test_smoke_and_store(self):
        c = cfg()
        df = make_synthetic_1m(days=16, seed=11)
        res = run_backtest(df, c, "5m", warmup_days=12, max_evals=120)
        self.assertIn("strategies", res)
        self.assertEqual(set(res["strategies"]), {"Trend Pullback", "Liquidity Sweep", "Breakout", "Reversal", "VWAP", "SMC OTE + FVG", "SMC Sweep-MSS-FVG", "Opening Range", "Round Number", "Liquidity Grab (PDH/PDL)", "Silver Bullet (Killzone OTE+FVG)"})
        j = Journal(os.path.join(tempfile.mkdtemp(), "j.sqlite"))
        n = store_probability_history(j, res)
        self.assertEqual(len(j.history_trades()), n)

    def test_save_report_handles_slash_in_strategy_label(self):
        """"Liquidity Grab (PDH/PDL)" contains a "/" — an unsanitized label made pandas try to
        write into a non-existent subdirectory, aborting the bootstrap subprocess with code 1."""
        def df(setup):
            return pd.DataFrame([{"ts": "2026-10-01T00:00:00+00:00", "r": 1.0, "outcome": "TP2", "setup": setup,
                                  "direction": "BUY", "session": "London", "regime": "BULLISH",
                                  "duration_min": 5.0, "score": 70.0}])
        books = {"Liquidity Grab (PDH/PDL)": df("Liquidity Grab (PDH/PDL)"), "ALL": df("x")}
        res = {"eval_tf": "15m", "strategies": {}, "combined": {}, "books": books}
        d = tempfile.mkdtemp()
        path = save_report(res, d)                              # لا يجب أن يرمي استثناء
        self.assertTrue(os.path.exists(path))
        self.assertTrue(any(f.startswith("trades_15m_Liquidity_Grab") and f.endswith(".csv")
                            for f in os.listdir(d)))
        self.assertTrue(all(os.sep not in f for f in os.listdir(d)))


class _Prov:
    """History provider stub: native 5m deeper than 1m, like free Yahoo (1m=28d, 5m=58d)."""
    def __init__(self, one_m, five_m, cap1=28, cap5=58):
        self._d = {"1m": one_m, "5m": five_m}
        self._cap = {"1m": cap1, "5m": cap5}
        self.calls = []

    def max_history_days(self, interval):
        return self._cap.get(interval)

    def fetch(self, interval, start, end):
        self.calls.append(interval)
        return self._d.get(interval, pd.DataFrame())


class TestBacktestBase(unittest.TestCase):
    NOW = pd.Timestamp("2026-10-15 12:00", tz="UTC")

    def _bars(self, start, periods, freq):
        idx = pd.date_range(start, periods=periods, freq=freq, tz="UTC")
        return pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1.0}, index=idx)

    def _load(self, prov, c):
        from goldbot.core.history_store import load_backtest_base
        return load_backtest_base(prov, "XAUUSD", c, 58, now=self.NOW)

    def test_stitches_native_5m_before_recent_1m(self):
        one = self._bars(self.NOW - pd.Timedelta(days=3), 60, "1min")    # recent 1m leg (inside 28d)
        five = self._bars(self.NOW - pd.Timedelta(days=40), 400, "5min")  # deeper 5m leg (older)
        base = self._load(_Prov(one, five), cfg(data__cache_dir=tempfile.mkdtemp()))
        self.assertLess(base.index[0], one.index[0])               # older 5m kept
        self.assertEqual(base.index[-1], one.index[-1])            # recent 1m kept
        self.assertFalse(base.index.has_duplicates)

    def test_falls_back_to_1m_when_no_deeper_5m(self):
        one = self._bars(self.NOW - pd.Timedelta(days=3), 60, "1min")
        prov = _Prov(one, self._bars(self.NOW - pd.Timedelta(days=40), 400, "5min"), cap1=28, cap5=28)
        base = self._load(prov, cfg(data__cache_dir=tempfile.mkdtemp()))
        self.assertEqual(base.index[0], one.index[0])

    def test_flag_disables_extension(self):
        one = self._bars(self.NOW - pd.Timedelta(days=3), 60, "1min")
        prov = _Prov(one, self._bars(self.NOW - pd.Timedelta(days=40), 400, "5min"))
        c = cfg(data__cache_dir=tempfile.mkdtemp(), backtest__extend_with_native_5m=False)
        base = self._load(prov, c)
        self.assertEqual(base.index[0], one.index[0])


if __name__ == "__main__":
    unittest.main()

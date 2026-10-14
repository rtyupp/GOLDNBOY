"""اختبارات حماية رأس المال: حد الخسارة اليومي، سلسلة الخسائر، وحجم المركز (بلا شبكة)."""
import os
import tempfile
import unittest

import pandas as pd

from goldbot.engine import no_trade
from goldbot.engine.risk import position_size
from goldbot.models import RiskPlan
from goldbot.tracking.journal import Journal
from tests.helpers import cfg


def _journal():
    return Journal(os.path.join(tempfile.mkdtemp(), "j.sqlite"))


def _closed(j, r, closed_ts):
    sid = j.add_signal(timestamp="2026-10-13T08:00:00+00:00", symbol="XAUUSD", direction="BUY", entry=2650.0,
                       sl=2645.0, tp1=2656.0, tp2=2662.0, strategy="Liquidity Sweep", session="New York",
                       score=80, historical_probability=None, prob_n=0, risk_level="Low", reason="x",
                       fingerprint={}, rr1=1.2, rr2=2.4)
    j.transition(sid, "OPEN", status="CLOSED", result="SL" if r <= 0 else "TP2", r=r, duration_min=10.0,
                 closed_ts=closed_ts)
    return sid


class TestJournalRiskQueries(unittest.TestCase):
    def test_realized_r_since_only_counts_closed_after_start(self):
        j = _journal()
        _closed(j, -1.0, "2026-10-12T20:00:00+00:00")     # أمس: لا يُحتسب
        _closed(j, -1.0, "2026-10-13T09:00:00+00:00")     # اليوم
        _closed(j, +0.5, "2026-10-13T11:00:00+00:00")     # اليوم
        self.assertAlmostEqual(j.realized_r_since(pd.Timestamp("2026-10-13T00:00:00+00:00")), -0.5)

    def test_consecutive_losses_resets_on_win(self):
        j = _journal()
        _closed(j, -1.0, "2026-10-13T09:00:00+00:00")
        _closed(j, -1.0, "2026-10-13T10:00:00+00:00")
        _closed(j, +1.0, "2026-10-13T11:00:00+00:00")     # رابحة تصفّر السلسلة
        self.assertEqual(j.consecutive_losses(), 0)
        _closed(j, -1.0, "2026-10-13T12:00:00+00:00")
        _closed(j, -0.5, "2026-10-13T13:00:00+00:00")
        self.assertEqual(j.consecutive_losses(), 2)


class TestCapitalGates(unittest.TestCase):
    def test_daily_loss_limit_blocks(self):
        j = _journal()
        _closed(j, -3.0, "2026-10-13T09:00:00+00:00")
        c = cfg(signals__max_daily_loss_r=3)
        r = no_trade.capital_gates(c, j, pd.Timestamp("2026-10-13T12:00:00+00:00"))
        self.assertTrue(any(x.startswith("DAILY_LOSS_LIMIT") for x in r))

    def test_daily_loss_limit_allows_below_threshold(self):
        j = _journal()
        _closed(j, -1.0, "2026-10-13T09:00:00+00:00")
        c = cfg(signals__max_daily_loss_r=3)
        self.assertEqual(no_trade.capital_gates(c, j, pd.Timestamp("2026-10-13T12:00:00+00:00")), [])

    def test_daily_limit_disabled_by_default(self):
        j = _journal()
        _closed(j, -5.0, "2026-10-13T09:00:00+00:00")
        self.assertEqual(no_trade.capital_gates(cfg(), j, pd.Timestamp("2026-10-13T12:00:00+00:00")), [])

    def test_loss_streak_blocks(self):
        j = _journal()
        for h in (9, 10, 11):
            _closed(j, -1.0, f"2026-10-13T{h}:00:00+00:00")
        c = cfg(signals__max_consecutive_losses=3)
        r = no_trade.capital_gates(c, j, pd.Timestamp("2026-10-13T12:00:00+00:00"))
        self.assertTrue(any(x.startswith("LOSS_STREAK") for x in r))

    def test_no_journal_is_safe(self):
        self.assertEqual(no_trade.capital_gates(cfg(signals__max_daily_loss_r=1), None,
                                                pd.Timestamp("2026-10-13T12:00:00+00:00")), [])


class TestPositionSize(unittest.TestCase):
    def _plan(self, risk=5.0):
        return RiskPlan("BUY", 2650.0, 2645.0, 2656.0, 2662.0, risk, 6.0, 12.0, 1.2, 2.4)

    def test_lots_from_balance_and_risk(self):
        # رصيد 10000، مخاطرة 1% = 100$، وقف 5$ => 20 أونصة = 0.2 لوت
        sz = position_size(self._plan(5.0), 10000.0, 1.0)
        self.assertAlmostEqual(sz["risk_cash"], 100.0)
        self.assertAlmostEqual(sz["oz"], 20.0)
        self.assertAlmostEqual(sz["lots"], 0.2)

    def test_none_when_balance_zero(self):
        self.assertIsNone(position_size(self._plan(), 0.0, 1.0))

    def test_none_when_risk_pct_zero(self):
        self.assertIsNone(position_size(self._plan(), 10000.0, 0.0))


class TestSignalIntegration(unittest.TestCase):
    def test_apply_limits_blocks_tradable_after_daily_loss(self):
        """مسار حقيقي: إعداد قابل للتداول يُمنع فعليًا عند بلوغ حد الخسارة اليومي."""
        from tests.test_e2e import find_tradable
        from tests.helpers import mkapp
        _df, cand = find_tradable()
        self.assertIsNotNone(cand)
        c, app = mkapp(signals__max_daily_loss_r=3)
        _closed(app.journal, -3.0, "2026-10-13T09:00:00+00:00")
        cand.reasons = []
        app._apply_limits(cand, pd.Timestamp("2026-10-13T12:00:00+00:00"))
        self.assertTrue(any(x.startswith("DAILY_LOSS_LIMIT") for x in cand.reasons))
        self.assertEqual(cand.log.final, "NO TRADE")
        self.assertFalse(cand.tradable)

    def test_size_line_present_when_balance_set(self):
        from tests.test_e2e import find_tradable
        from tests.helpers import mkapp
        _df, cand = find_tradable()
        c, app = mkapp(risk__account_balance=10000, risk__risk_per_trade_pct=1.0)
        line = app._size_line(cand.best.plan)
        self.assertIn("لوت", line)
        self.assertIn("$", line)

    def test_size_line_empty_without_balance(self):
        from tests.test_e2e import find_tradable
        from tests.helpers import mkapp
        _df, cand = find_tradable()
        c, app = mkapp()                     # account_balance=0 افتراضيًا
        self.assertEqual(app._size_line(cand.best.plan), "")


if __name__ == "__main__":
    unittest.main()

"""اختبارات: تقرير صحة صادق، حارس توقف التاريخ العميق، وتجاهل العطلة في انتهاء مدة الصفقة."""
import os
import tempfile
import unittest
from unittest import mock

import pandas as pd

from goldbot.models import Tick
from goldbot.tracking.journal import Journal
from goldbot.tracking.tracker import TradeTracker, _trading_minutes
from tests.helpers import cfg, mkapp


def _closed(j, ts="2026-06-01T10:00:00+00:00"):
    return j.add_signal(timestamp=ts, symbol="XAUUSD", direction="BUY", entry=2650.0, sl=2645.0,
                        tp1=2656.0, tp2=2662.0, strategy="Liquidity Sweep", session="New York", score=80,
                        historical_probability=None, prob_n=0, risk_level="Low", reason="x",
                        fingerprint={"setup": "Liquidity Sweep", "direction": "BUY", "session": "New York",
                                     "htf_bias": "bull", "structure": "BULLISH/BOS", "atr_bucket": "mid",
                                     "rsi_bucket": "50-65", "liquidity": "sweep", "regime": "BULLISH"},
                        rr1=1.2, rr2=2.4)


class TestTradingMinutes(unittest.TestCase):
    def test_normal_day_is_wall_clock(self):
        a = pd.Timestamp("2026-06-01T08:00Z")      # الاثنين
        b = pd.Timestamp("2026-06-01T12:00Z")
        self.assertAlmostEqual(_trading_minutes(a, b), 240.0)

    def test_weekend_hours_are_excluded(self):
        # من الجمعة 20:00 إلى الأحد 23:00: التداول الفعلي = ساعة الجمعة (20→21) + ساعة الأحد (22→23)
        a = pd.Timestamp("2026-06-05T20:00Z")      # الجمعة
        b = pd.Timestamp("2026-06-07T23:00Z")      # الأحد
        self.assertAlmostEqual(_trading_minutes(a, b), 120.0)

    def test_spanning_whole_weekend(self):
        a = pd.Timestamp("2026-06-05T22:00Z")      # الجمعة بعد الإغلاق
        b = pd.Timestamp("2026-06-07T21:00Z")      # الأحد قبل الافتتاح
        self.assertAlmostEqual(_trading_minutes(a, b), 0.0)


class TestWeekendExpiry(unittest.TestCase):
    def test_trade_is_not_expired_merely_by_the_weekend(self):
        j = Journal(os.path.join(tempfile.mkdtemp(), "j.sqlite"))
        sid = _closed(j, ts="2026-06-05T20:00:00+00:00")            # الجمعة
        tr = TradeTracker(j, [], cfg(tracking__max_trade_minutes=1440))   # 24h
        tr.check_expiry(pd.Timestamp("2026-06-07T20:00:00Z"), 2652.0, 2652.3)   # الأحد قبل الافتتاح
        self.assertEqual(j.get(sid)["status"], "OPEN")              # 48h ساعة حائط لكن دقائق تداول قليلة

    def test_trade_expires_on_trading_minutes_not_wall_clock(self):
        j = Journal(os.path.join(tempfile.mkdtemp(), "j.sqlite"))
        sid = _closed(j, ts="2026-06-01T10:00:00+00:00")            # الاثنين
        msgs = []
        tr = TradeTracker(j, msgs.append, cfg(tracking__max_trade_minutes=1440))
        tr.check_expiry(pd.Timestamp("2026-06-02T11:00:00Z"), 2652.0, 2652.3)   # +25h تداول
        self.assertEqual(j.get(sid)["result"], "EXPIRED")
        self.assertTrue(msgs)


class TestDeepHistoryStatus(unittest.TestCase):
    def test_stale_history_flagged_outside_market_hours(self):
        c, app = mkapp()
        app.md.load_history(pd.DataFrame(
            {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1.0]},
            index=pd.DatetimeIndex([pd.Timestamp("2026-06-01T10:00Z")])))
        with mock.patch("goldbot.app.pd.Timestamp.now", return_value=pd.Timestamp("2026-06-02T10:00Z")):
            ok, why, age = app.deep_history_status(pd.Timestamp("2026-06-02T10:00Z"))
        self.assertFalse(ok)
        self.assertTrue(why.startswith("HISTORY_STALE"))
        self.assertGreater(age, 1400)

    def test_market_closed_is_not_stale(self):
        c, app = mkapp()
        app.md.load_history(pd.DataFrame(
            {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1.0]},
            index=pd.DatetimeIndex([pd.Timestamp("2026-06-05T20:00Z")])))
        ok, why, _age = app.deep_history_status(pd.Timestamp("2026-06-07T10:00Z"))   # الأحد: السوق مغلق
        self.assertTrue(ok)


class TestHealthSnapshot(unittest.TestCase):
    def _app_with_tick(self, connected=True, age_ok=True):
        """التكات تُبنى بساعة النظام الحقيقية (TickStore يستخدم time.time)، لذا نُبني الطابع الزمني منها
        ونضبط عمر «التاريخ العميق» بنفس الساعة."""
        c, app = mkapp()
        app.ticks.set_connected(connected)
        import time as _t
        now = pd.Timestamp.now(tz="UTC")
        now_ms = int(_t.time() * 1000)
        ts = now_ms if age_ok else now_ms - 600_000
        app.ticks.on_tick(Tick(ts, 2650.0, 2650.2, 2650.1, ts))
        app.md.load_history(pd.DataFrame(
            {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1.0]},
            index=pd.DatetimeIndex([now.floor("1min")])))
        return app

    def test_ok_true_when_fresh_and_history_current(self):
        app = self._app_with_tick()
        snap = app.health_snapshot()
        self.assertTrue(snap["ok"])
        self.assertTrue(snap["data_fresh"])
        self.assertTrue(snap["deep_history_ok"])

    def test_ok_false_when_tick_stale(self):
        app = self._app_with_tick(age_ok=False)
        snap = app.health_snapshot()
        self.assertFalse(snap["ok"])
        self.assertFalse(snap["data_fresh"])

    def test_ok_false_when_disconnected(self):
        app = self._app_with_tick(connected=False)
        snap = app.health_snapshot()
        self.assertFalse(snap["ok"])
        self.assertIn("WS_DISCONNECTED", snap["reasons"])

    def test_market_closed_is_ok(self):
        app = self._app_with_tick(age_ok=False)
        with mock.patch("goldbot.app.resources.market_closed", return_value=True):
            snap = app.health_snapshot()
        self.assertTrue(snap["market_closed"])
        self.assertTrue(snap["ok"])


if __name__ == "__main__":
    unittest.main()

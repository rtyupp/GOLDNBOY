"""اختبارات: عتبة التقاطع التكيّفية والسبريد المتغيّر حسب الجلسة/الأخبار في الـ Backtest."""
import os
import tempfile
import unittest

import pandas as pd

from goldbot.tracking.journal import Journal
from goldbot.engine import no_trade
from goldbot.backtest.engine import _spread_for
from goldbot.analysis.sessions import DEFAULT_SESSIONS
from tests.helpers import cfg


def _journal_with_closed(rs, start="2026-06-01T10:00:00+00:00"):
    """سجل فيه صفقات مغلقة بقيم R وترتيب إغلاق زمني صريح."""
    j = Journal(os.path.join(tempfile.mkdtemp(), "j.sqlite"))
    t0 = pd.Timestamp(start)
    for i, r in enumerate(rs):
        sid = j.add_signal(timestamp=(t0 + pd.Timedelta(hours=i)).isoformat(), symbol="XAUUSD", direction="BUY",
                           entry=2650.0, sl=2645.0, tp1=2656.0, tp2=2662.0, strategy="Liquidity Sweep",
                           session="New York", score=80, historical_probability=None, prob_n=0, risk_level="Low",
                           reason="x", fingerprint={"setup": "Liquidity Sweep", "direction": "BUY",
                           "session": "New York", "htf_bias": "bull", "structure": "BULLISH/BOS",
                           "atr_bucket": "mid", "rsi_bucket": "50-65", "liquidity": "sweep", "regime": "BULLISH"},
                           rr1=1.2, rr2=2.4)
        j.transition(sid, "OPEN", status="CLOSED", result="TP2" if r > 0 else "SL", r=float(r),
                     duration_min=10.0, closed_ts=(t0 + pd.Timedelta(hours=i)).isoformat())
    return j


class TestRecentAvgR(unittest.TestCase):
    def test_empty_journal(self):
        j = Journal(os.path.join(tempfile.mkdtemp(), "j.sqlite"))
        self.assertEqual(j.recent_avg_r(20), (0.0, 0))

    def test_tail_average(self):
        j = _journal_with_closed([1.0, -1.0, 2.0, -1.0])
        avg, n = j.recent_avg_r(2)
        self.assertEqual(n, 2)
        self.assertAlmostEqual(avg, 0.5)          # (2.0 + -1.0)/2


class TestAdaptiveMinScore(unittest.TestCase):
    def test_disabled_by_default(self):
        c = cfg()
        self.assertEqual(no_trade.adaptive_min_score(c, _journal_with_closed([-1.0] * 20)),
                         float(c.get("confluence.min_score", 70)))

    def test_no_journal_returns_base(self):
        c = cfg(confluence__adaptive_step=5)
        self.assertEqual(no_trade.adaptive_min_score(c, None), float(c.get("confluence.min_score", 70)))

    def test_raises_when_recent_edge_negative(self):
        c = cfg(confluence__adaptive_step=5, confluence__adaptive_lookback=10, confluence__adaptive_min_samples=5)
        base = float(c.get("confluence.min_score", 70))
        self.assertAlmostEqual(no_trade.adaptive_min_score(c, _journal_with_closed([-1.0] * 10)), base + 5)

    def test_stays_base_when_recent_edge_positive(self):
        c = cfg(confluence__adaptive_step=5, confluence__adaptive_min_samples=5)
        base = float(c.get("confluence.min_score", 70))
        self.assertAlmostEqual(no_trade.adaptive_min_score(c, _journal_with_closed([1.0] * 10)), base)

    def test_small_sample_does_not_raise(self):
        c = cfg(confluence__adaptive_step=5, confluence__adaptive_min_samples=10)
        base = float(c.get("confluence.min_score", 70))
        self.assertAlmostEqual(no_trade.adaptive_min_score(c, _journal_with_closed([-1.0] * 3)), base)

    def test_bump_is_capped(self):
        c = cfg(confluence__adaptive_step=20, confluence__adaptive_max_bump=5, confluence__adaptive_min_samples=5)
        base = float(c.get("confluence.min_score", 70))
        self.assertAlmostEqual(no_trade.adaptive_min_score(c, _journal_with_closed([-1.0] * 10)), base + 5)


class _Best:
    signal = "BUY"


class TestSetupGatesAdaptive(unittest.TestCase):
    def test_gate_uses_raised_threshold(self):
        c = cfg(confluence__adaptive_step=5, confluence__adaptive_min_samples=5)
        j = _journal_with_closed([-1.0] * 10)
        base = float(c.get("confluence.min_score", 70))
        # درجة فوق الأساس لكن تحت الأساس+5 => تُحجب بالعتبة التكيّفية فقط
        reasons = no_trade.setup_gates(c, _Best(), base + 2, None, j)
        self.assertTrue(any(r.startswith("LOW_CONFLUENCE") for r in reasons))
        # بدون السجل لا تُرفع العتبة => تمر
        self.assertEqual(no_trade.setup_gates(c, _Best(), base + 2, None, None), [])


class TestSpreadFor(unittest.TestCase):
    def setUp(self):
        self.sess = DEFAULT_SESSIONS

    def test_flat_without_multipliers(self):
        c = cfg()
        t = pd.Timestamp("2026-06-01T14:00Z")     # London/New York
        self.assertAlmostEqual(_spread_for(t, 0.3, c, self.sess), 0.3)

    def test_session_multiplier_applied(self):
        c = cfg(backtest__spread_session_mult={"Off-hours": 3.0, "London/New York": 1.0})
        off = pd.Timestamp("2026-06-01T02:00Z")   # آسيا... نختار ساعة خارج الجلسات فعلاً
        # 02:00 UTC داخل آسيا (0-8) => Asian
        self.assertAlmostEqual(_spread_for(off, 0.3, c, self.sess), 0.3)
        dead = pd.Timestamp("2026-06-01T22:30Z")  # 22:30 خارج كل الجلسات => Off-hours
        self.assertAlmostEqual(_spread_for(dead, 0.3, c, self.sess), 0.9)

    def test_news_window_multiplier(self):
        c = cfg(backtest__spread_news_mult=4.0, backtest__news_window_min=30)
        ev = pd.Timestamp("2026-06-01T14:00Z")
        near = pd.Timestamp("2026-06-01T14:20Z")
        far = pd.Timestamp("2026-06-01T15:30Z")
        self.assertAlmostEqual(_spread_for(near, 0.3, c, self.sess, [ev]), 1.2)
        self.assertAlmostEqual(_spread_for(far, 0.3, c, self.sess, [ev]), 0.3)

    def test_session_and_news_stack(self):
        c = cfg(backtest__spread_session_mult={"London/New York": 2.0},
                backtest__spread_news_mult=3.0, backtest__news_window_min=30)
        ev = pd.Timestamp("2026-06-01T14:00Z")
        self.assertAlmostEqual(_spread_for(pd.Timestamp("2026-06-01T14:05Z"), 0.3, c, self.sess, [ev]), 1.8)


if __name__ == "__main__":
    unittest.main()

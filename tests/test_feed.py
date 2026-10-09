import asyncio, json, time, unittest
import pandas as pd
from goldbot.models import Tick
from goldbot.core.tick_store import TickStore
from goldbot.core.candle_builder import CandleBuilder
from goldbot.core.timeframes import resample_ohlcv, closed_only


class TestFreshness(unittest.TestCase):
    def mk(self, now):
        return TickStore(max_age_sec=10, clock=lambda: now[0])

    def test_stale_and_disconnect_are_no_trade(self):
        now = [1000.0]
        s = self.mk(now)
        self.assertEqual(s.check_fresh(), (False, "NO_TICK_YET"))
        s.set_connected(True)
        s.on_tick(Tick(1_000_000, 1.0, 1.1, 1.05, 1_000_000))
        self.assertTrue(s.check_fresh()[0])
        now[0] = 1011.0
        ok, why = s.check_fresh()
        self.assertFalse(ok)
        self.assertTrue(why.startswith("STALE_DATA"))
        now[0] = 1001.0
        s.set_connected(False)
        self.assertEqual(s.check_fresh(), (False, "WS_DISCONNECTED"))

    def test_old_source_timestamp_is_stale_even_if_just_received(self):
        now = [1000.0]
        s = self.mk(now)
        s.set_connected(True)
        s.on_tick(Tick(900_000, 1.0, 1.1, 1.05, 1_000_000))      # snapshot 100s old, received now
        self.assertFalse(s.check_fresh()[0])

    def test_out_of_order_ignored(self):
        now = [1000.0]
        s = self.mk(now)
        s.on_tick(Tick(2000, 1, 1.1, 1, 2000))
        s.on_tick(Tick(1000, 9, 9.1, 9, 2001))
        self.assertEqual(s.last.bid, 1)

    def test_market_data_replacing_live_minute_keeps_live_count(self):
        from goldbot.core.market_data import MarketData
        from goldbot.core.candle_builder import Candle

        md = MarketData()
        t0 = 1_700_000_000_000
        md.add_closed(Candle(t0, 100, 101, 99, 100.5, 1, None))
        md.add_closed(Candle(t0, 100, 102, 98, 101, 2, None))
        self.assertEqual(md.live_candles, 1)
        self.assertEqual(len(md.base), 1)


class TestCandles(unittest.TestCase):
    def test_builds_1m_and_flushes(self):
        closed = []
        b = CandleBuilder(closed.append)
        base = 1_780_000_000_000 // 60000 * 60000
        for dt, px in [(1000, 10.0), (20000, 12.0), (40000, 9.0), (59000, 11.0)]:
            b.on_tick(Tick(base + dt, px - 0.1, px + 0.1, px, base + dt))
        self.assertEqual(closed, [])
        b.on_tick(Tick(base + 61000, 11.5, 11.7, 11.6, base + 61000))
        self.assertEqual(len(closed), 1)
        c = closed[0]
        self.assertEqual((c.open, c.high, c.low, c.close, c.volume), (10.0, 12.0, 9.0, 11.0, 4))
        b.on_tick(Tick(base + 5000, 1, 1.1, 1, 1))                # late tick must not rewrite the past
        self.assertEqual(len(closed), 1)
        b.flush(base + 200000)
        self.assertEqual(len(closed), 2)

    def test_closed_only_never_returns_forming_candle(self):
        idx = pd.date_range("2026-06-01 00:00", periods=30, freq="1min", tz="UTC")
        df = pd.DataFrame({"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1.0}, index=idx)
        f5 = resample_ohlcv(df, "5m")
        as_of = pd.Timestamp("2026-06-01 00:17:00", tz="UTC")
        out = closed_only(f5, as_of)
        self.assertEqual(out.index[-1], pd.Timestamp("2026-06-01 00:10", tz="UTC"))   # 00:15 candle still forming
        self.assertTrue((out["close_time"] <= as_of).all())

    def test_all_timeframes_resample(self):
        from goldbot.providers.synthetic import make_synthetic_1m
        from goldbot.core.timeframes import ALL_TFS
        df = make_synthetic_1m(days=10)
        for tf in ALL_TFS:
            r = resample_ohlcv(df, tf)
            self.assertTrue((r["high"] >= r["low"]).all())
        r4 = resample_ohlcv(df, "4H")
        self.assertEqual(r4.index[0].hour % 4, 0)


if __name__ == "__main__":
    unittest.main()

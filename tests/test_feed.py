import asyncio, json, time, unittest
import pandas as pd
from tests.helpers import FakeWS, FakeConn, cfg
from goldbot.models import Tick
from goldbot.providers.siftingio_ws import SiftingWSProvider, parse_tick
from goldbot.core.tick_store import TickStore
from goldbot.core.candle_builder import CandleBuilder
from goldbot.core.timeframes import resample_ohlcv, closed_only


def tickmsg(t, b=2650.0, a=2650.3, p=2650.15, s="XAUUSD"):
    return {"f": "tick", "class": "com", "s": s, "p": p, "b": b, "a": a, "t": t}


class TestParse(unittest.TestCase):
    def test_ok(self):
        t = parse_tick(tickmsg(1000), "XAUUSD", 2000)
        self.assertEqual((t.bid, t.ask, t.ts_ms), (2650.0, 2650.3, 1000))
        self.assertAlmostEqual(t.spread, 0.3)

    def test_wrong_symbol_and_junk(self):
        self.assertIsNone(parse_tick(tickmsg(1, s="XAGUSD"), "XAUUSD", 1))
        self.assertIsNone(parse_tick({"f": "pong"}, "XAUUSD", 1))
        self.assertIsNone(parse_tick({"f": "tick", "s": "XAUUSD", "t": 1}, "XAUUSD", 1))

    def test_crossed_quote_is_not_a_quote(self):
        t = parse_tick(tickmsg(1, b=2651, a=2650), "XAUUSD", 1)
        self.assertFalse(t.has_quote)


class TestWS(unittest.IsolatedAsyncioTestCase):
    async def test_subscribe_ping_tick_and_reconnect(self):
        got, states, conns = [], [], []
        now_ms = int(time.time() * 1000)
        frames1 = [{"f": "ack", "op": "auth", "tier": "free", "max_conn": 1, "max_subs": 5}, tickmsg(now_ms), "not json"]
        frames2 = [tickmsg(now_ms + 1000)]
        seq = [FakeWS(frames1), FakeWS(frames2)]

        def connect(url):
            conns.append(url)
            if not seq:
                raise ConnectionError("done")
            return FakeConn(seq.pop(0))
        c = {"ping_interval_sec": 0.01, "reconnect_min_sec": 0.01, "reconnect_max_sec": 0.02}
        p = SiftingWSProvider(c, "KEY123", "XAUUSD", got.append, states.append, connect=connect)
        task = asyncio.create_task(p.run())
        for _ in range(200):
            await asyncio.sleep(0.02)
            if len(got) >= 2:
                break
        p.stop()
        task.cancel()
        self.assertGreaterEqual(len(got), 2)                     # reconnected automatically
        self.assertIn("key=KEY123", conns[0])
        self.assertGreaterEqual(len(conns), 2)
        self.assertTrue(any(s is False for s in states))         # disconnect reported
        self.assertTrue(any(s is True for s in states))

    async def test_subscribe_frame_and_pings(self):
        ws = FakeWS([], close_after=False)
        p = SiftingWSProvider({"ping_interval_sec": 0.01}, "K", "XAUUSD", lambda t: None, connect=lambda u: FakeConn(ws))
        t = asyncio.create_task(p._session(ws))
        await asyncio.sleep(0.08)
        p.stop()
        t.cancel()
        self.assertEqual(ws.sent[0], {"op": "subscribe", "product": "com", "symbols": ["XAUUSD"]})
        self.assertTrue(any(m == {"op": "ping"} for m in ws.sent[1:]))

    async def test_auth_error_flagged(self):
        p = SiftingWSProvider({}, "bad", "XAUUSD", lambda t: None)
        p._handle(json.dumps({"f": "error", "code": "auth_failed", "message": "x"}))
        self.assertTrue(p.fatal_auth)


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

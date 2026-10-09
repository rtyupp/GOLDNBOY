import unittest
import pandas as pd
from goldbot.providers.oanda import OandaHistory, OandaStream


class Resp:
    status_code = 200
    text = ""
    def __init__(self, payload):
        self.payload = payload
    def json(self):
        return self.payload


class Session:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return Resp(self.payload)


class TestOanda(unittest.TestCase):
    def test_history_parses_complete_utc_candles(self):
        s = Session({"candles": [
            {"complete": True, "time": "2026-10-08T23:00:00.000000000Z", "volume": 7,
             "mid": {"o": "2650.1", "h": "2651.0", "l": "2649.8", "c": "2650.7"}},
            {"complete": False, "time": "2026-10-08T23:01:00.000000000Z", "volume": 1,
             "mid": {"o": "2650.7", "h": "2651.2", "l": "2650.5", "c": "2651.0"}},
        ]})
        p = OandaHistory({}, "TOKEN", "ACCOUNT", session=s)
        df = p.fetch("1m", pd.Timestamp("2026-10-08 23:00", tz="UTC"), pd.Timestamp("2026-10-08 23:02", tz="UTC"))
        self.assertEqual(len(df), 1)
        self.assertTrue(df.index.is_monotonic_increasing)
        self.assertEqual(float(df.iloc[0].close), 2650.7)
        self.assertEqual(s.calls[0][1]["params"]["granularity"], "M1")

    def test_stream_price_to_tick(self):
        p = OandaStream({}, "TOKEN", "ACCOUNT", "XAU_USD", lambda _: None)
        t = p._parse({"type": "PRICE", "instrument": "XAU_USD",
                      "time": "2026-10-08T23:00:00.000000000Z",
                      "bids": [{"price": "2650.10"}], "asks": [{"price": "2650.35"}]}, 123)
        self.assertIsNotNone(t)
        self.assertAlmostEqual(t.bid, 2650.10)
        self.assertAlmostEqual(t.ask, 2650.35)
        self.assertAlmostEqual(t.last, 2650.225)
        self.assertIsNone(p._parse({"type": "HEARTBEAT"}, 123))

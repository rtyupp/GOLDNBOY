import lzma
import struct
import unittest
import pandas as pd
from goldbot.providers.dukascopy import DukascopyHistory, FallbackHistory


class Empty:
    def fetch(self, interval, start, end):
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"], index=pd.DatetimeIndex([], tz="UTC"))


class Full:
    def fetch(self, interval, start, end):
        i = pd.date_range(start, periods=10, freq="min", tz="UTC")
        return pd.DataFrame({"open": 1., "high": 2., "low": 0.5, "close": 1.5, "volume": 1.}, index=i)


class TestDukascopy(unittest.TestCase):
    def test_decode_bi5_tick_record(self):
        hour = pd.Timestamp("2026-01-02 12:00", tz="UTC")
        raw = struct.pack(">IIIff", 1500, 2650123, 2650000, 2.0, 3.0)
        df = DukascopyHistory.decode_ticks(lzma.compress(raw), hour)
        self.assertEqual(len(df), 1)
        self.assertAlmostEqual(float(df.iloc[0].close), 2650.0615)
        self.assertEqual(float(df.iloc[0].volume), 5.0)

    def test_fallback_when_primary_is_empty(self):
        df = FallbackHistory(Empty(), Full()).fetch("1m", pd.Timestamp("2026-01-01", tz="UTC"), pd.Timestamp("2026-01-01 00:10", tz="UTC"))
        self.assertEqual(len(df), 10)

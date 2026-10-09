"""Free Dukascopy historical tick feed, converted to OHLCV bars.

Dukascopy publishes compressed hourly .bi5 files without an API key. Missing
hours (weekends, unavailable dates, or upstream errors) are ignored; the
factory can wrap this provider with SiftingIO as a fallback.
"""
from __future__ import annotations
import io
import lzma
import logging
import struct
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pandas as pd
import requests

from goldbot.core.timeframes import OHLCV, resample_ohlcv
from goldbot.providers.base import HistoryProvider

log = logging.getLogger("dukascopy")


class DukascopyHistory(HistoryProvider):
    def __init__(self, cfg: dict, symbol: str = "XAUUSD"):
        self.base = cfg.get("url", "http://datafeed.dukascopy.com/datafeed").rstrip("/")
        self.symbol = cfg.get("instrument", symbol.replace("/", "").upper())
        self.scale = float(cfg.get("price_scale", 1000))
        self.workers = max(1, min(12, int(cfg.get("workers", 8))))
        self.timeout = float(cfg.get("timeout_sec", 15))

    @staticmethod
    def decode_ticks(blob: bytes, hour: pd.Timestamp, scale: float = 1000) -> pd.DataFrame:
        raw = lzma.decompress(blob)
        rows = []
        for pos in range(0, len(raw) - 19, 20):
            ms, ask_i, bid_i, ask_vol, bid_vol = struct.unpack(">IIIff", raw[pos:pos + 20])
            bid, ask = bid_i / scale, ask_i / scale
            if bid <= 0 or ask <= 0 or ask < bid:
                continue
            ts = hour + pd.Timedelta(milliseconds=ms)
            rows.append((ts, (bid + ask) / 2.0, float(ask_vol or 0) + float(bid_vol or 0)))
        if not rows:
            return pd.DataFrame(columns=["close", "volume"], index=pd.DatetimeIndex([], tz="UTC"))
        return pd.DataFrame(rows, columns=["time", "close", "volume"]).set_index("time").sort_index()

    def _hour(self, hour: pd.Timestamp):
        url = f"{self.base}/{self.symbol}/{hour.year:04d}/{hour.month:02d}/{hour.day:02d}/{hour.hour:02d}h_ticks.bi5"
        try:
            r = requests.get(url, timeout=self.timeout)
            if r.status_code != 200 or not r.content:
                return pd.DataFrame(columns=["close", "volume"])
            return self.decode_ticks(r.content, hour, self.scale)
        except (requests.RequestException, lzma.LZMAError, struct.error, ValueError) as e:
            log.debug("Dukascopy hour unavailable %s: %s", hour, e)
            return pd.DataFrame(columns=["close", "volume"])

    def fetch(self, interval: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        start = pd.Timestamp(start)
        end = pd.Timestamp(end)
        start = start.tz_localize("UTC") if start.tzinfo is None else start.tz_convert("UTC")
        end = end.tz_localize("UTC") if end.tzinfo is None else end.tz_convert("UTC")
        hours = list(pd.date_range(start.floor("h"), end.ceil("h"), freq="h", inclusive="left", tz="UTC"))
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            parts = list(pool.map(self._hour, hours))
        ticks = [p for p in parts if not p.empty]
        if not ticks:
            return pd.DataFrame(columns=OHLCV, index=pd.DatetimeIndex([], tz="UTC"))
        t = pd.concat(ticks).sort_index()
        t = t[(t.index >= start) & (t.index < end)]
        if t.empty:
            return pd.DataFrame(columns=OHLCV, index=pd.DatetimeIndex([], tz="UTC"))
        bars = t["close"].resample("1min").ohlc()
        bars["volume"] = t["volume"].resample("1min").sum()
        bars = bars.dropna(subset=["open", "high", "low", "close"])
        bars = bars[OHLCV]
        if interval != "1m":
            tf = {"5m": "5min", "15m": "15min", "30m": "30min", "1h": "1h", "1d": "1D"}.get(interval)
            if tf:
                bars = resample_ohlcv(bars, tf)[OHLCV]
        return bars.astype(float)


class FallbackHistory(HistoryProvider):
    def __init__(self, primary: HistoryProvider, fallback: HistoryProvider):
        self.primary, self.fallback = primary, fallback

    def fetch(self, interval: str, start, end) -> pd.DataFrame:
        df = self.primary.fetch(interval, start, end)
        minutes = max(1, int((pd.Timestamp(end) - pd.Timestamp(start)).total_seconds() / 60))
        factor = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "1d": 1440}.get(interval, 1)
        expected = max(10, int((minutes / factor) * 0.25))
        if len(df) >= expected:
            return df
        log.warning("Dukascopy returned %d/%d bars; using fallback history provider", len(df), expected)
        return self.fallback.fetch(interval, start, end)

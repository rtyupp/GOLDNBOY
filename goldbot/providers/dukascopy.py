"""Free Dukascopy historical tick feed, converted to OHLCV bars (no API key, no account).

Dukascopy publishes compressed hourly .bi5 files. IMPORTANT: in the URL the month is ZERO-BASED
(January = 00 ... December = 11). Missing hours (weekends, the still-open current hour, upstream
errors) are skipped; a missing hour is never treated as success by FallbackHistory.
"""
from __future__ import annotations
import io
import lzma
import logging
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pandas as pd
import requests

from goldbot.core.timeframes import OHLCV, resample_ohlcv
from goldbot.providers.base import HistoryProvider

log = logging.getLogger("dukascopy")


class DukascopyHistory(HistoryProvider):
    def __init__(self, cfg: dict, symbol: str = "XAUUSD", session: requests.Session | None = None):
        self.base = cfg.get("url", "https://datafeed.dukascopy.com/datafeed").rstrip("/")
        # second attempt on the other scheme if the first one fails at the network level
        self.alt_base = self.base.replace("https://", "http://", 1) if self.base.startswith("https://") \
            else self.base.replace("http://", "https://", 1)
        self.symbol = cfg.get("instrument", symbol.replace("/", "").upper())
        self.scale = float(cfg.get("price_scale", 1000))
        self.workers = max(1, min(12, int(cfg.get("workers", 6))))
        self.timeout = float(cfg.get("timeout_sec", 15))
        self.retries = max(1, int(cfg.get("retries", 3)))
        self.s = session or requests.Session()
        self.s.headers.setdefault("User-Agent", "Mozilla/5.0 goldbot")
        self._lock = threading.Lock()
        self.progress = (0, 0)                    # (hours done, hours total) of the current fetch - shown in /status
        self.stats = {"ok": 0, "missing": 0, "errors": 0}
        self.last_error: str | None = None

    def _count(self, key: str, err: str | None = None):
        with self._lock:
            self.stats[key] += 1
            if err:
                self.last_error = err
            self.progress = (self.progress[0] + 1, self.progress[1])

    def url_for(self, hour: pd.Timestamp, base: str | None = None) -> str:
        # Dukascopy month folders are 00..11 (zero-based) - month-1 is required
        return (f"{base or self.base}/{self.symbol}/{hour.year:04d}/{hour.month - 1:02d}/"
                f"{hour.day:02d}/{hour.hour:02d}h_ticks.bi5")

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
        empty = pd.DataFrame(columns=["close", "volume"])
        err = None
        for base in (self.base, self.alt_base):
            for attempt in range(self.retries):
                try:
                    r = self.s.get(self.url_for(hour, base), timeout=self.timeout)
                except requests.RequestException as e:
                    err = f"{type(e).__name__}"
                    time.sleep(0.4 * (attempt + 1))
                    continue
                if r.status_code == 404:                  # hour not published (weekend / still open / not yet available)
                    self._count("missing")
                    return empty
                if r.status_code == 200:
                    if not r.content:
                        self._count("missing")            # market closed: empty file
                        return empty
                    try:
                        out = self.decode_ticks(r.content, hour, self.scale)
                        self._count("ok")
                        return out
                    except (lzma.LZMAError, struct.error, ValueError) as e:
                        self._count("errors", f"corrupt file: {type(e).__name__}")
                        return empty
                err = f"HTTP {r.status_code}"             # 403 / 429 / 5xx: blocked or throttled -> back off, retry
                time.sleep(0.8 * (attempt + 1))
        self._count("errors", err)
        return empty

    def fetch(self, interval: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        start = pd.Timestamp(start)
        end = pd.Timestamp(end)
        start = start.tz_localize("UTC") if start.tzinfo is None else start.tz_convert("UTC")
        end = end.tz_localize("UTC") if end.tzinfo is None else end.tz_convert("UTC")
        hours = list(pd.date_range(start.floor("h"), end.ceil("h"), freq="h", inclusive="left", tz="UTC"))
        self.stats = {"ok": 0, "missing": 0, "errors": 0}
        self.progress = (0, len(hours))
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            parts = list(pool.map(self._hour, hours))
        ticks = [p for p in parts if not p.empty]
        if not ticks and self.stats["errors"] > 0 and self.stats["errors"] >= self.stats["missing"]:
            # do NOT pretend "no data": the source is unreachable/blocked -> surface the real reason
            raise RuntimeError(f"Dukascopy غير متاح ({self.stats['errors']} فشل من {len(hours)} ساعة، آخر خطأ: {self.last_error})")
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

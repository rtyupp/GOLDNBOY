"""SiftingIO REST history (https://sifting.io/docs/historical/commodities).

GET /v1/hist/commodities/:symbol/bars?start&end&interval&limit&cursor   (X-API-Key, gzip)
Intervals: 1m 5m 15m 30m 1h 1d 1w 1mo.  v = tick count.  Free plan: 10,000 REST calls / month,
so results are cached on disk and only the missing tail is downloaded.
"""
from __future__ import annotations
import logging, os, time
import pandas as pd
import requests
from goldbot.providers.base import HistoryProvider
from goldbot.core.timeframes import OHLCV

log = logging.getLogger("sifting_rest")
INTERVALS = {"1m", "5m", "15m", "30m", "1h", "1d"}


class SiftingRestHistory(HistoryProvider):
    def __init__(self, cfg: dict, api_key: str, symbol: str, session: requests.Session | None = None):
        self.base = cfg.get("rest_url", "https://api.sifting.io").rstrip("/")
        self.key = api_key
        self.symbol = symbol.upper()
        self.s = session or requests.Session()
        self.page = int(cfg.get("page_limit", 2000))
        self.max_calls = int(cfg.get("max_calls_per_fetch", 80))
        self.calls = 0

    def _get(self, url, params):
        for attempt in range(4):
            r = self.s.get(url, params=params, headers={"X-API-Key": self.key, "Accept-Encoding": "gzip"}, timeout=30)
            self.calls += 1
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 503):
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"sifting REST {r.status_code}: {r.text[:200]}")
        raise RuntimeError("sifting REST: retries exhausted")

    def fetch(self, interval: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        if interval not in INTERVALS:
            raise ValueError(f"interval {interval} unsupported by REST")
        url = f"{self.base}/v1/hist/commodities/{self.symbol}/bars"
        params = {"start": pd.Timestamp(start).strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "end": pd.Timestamp(end).strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "interval": interval, "limit": self.page, "order": "asc"}
        rows, cursor, n = [], None, 0
        while n < self.max_calls:
            p = {"cursor": cursor} if cursor else params
            j = self._get(url, p)
            rows.extend(j.get("data", []))
            cursor = (j.get("meta") or {}).get("next_cursor")
            n += 1
            if not cursor:
                break
        if not rows:
            return pd.DataFrame(columns=OHLCV, index=pd.DatetimeIndex([], tz="UTC"))
        df = pd.DataFrame(rows)
        df.index = pd.to_datetime(df["t"], unit="ms", utc=True)
        df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})[OHLCV]
        return df.astype(float)

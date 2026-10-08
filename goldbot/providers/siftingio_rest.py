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
        start, end = pd.Timestamp(start), pd.Timestamp(end)
        if start.tzinfo is None:
            start = start.tz_localize("UTC")
        else:
            start = start.tz_convert("UTC")
        if end.tzinfo is None:
            end = end.tz_localize("UTC")
        else:
            end = end.tz_convert("UTC")

        fetch_limit = self.calls + self.max_calls

        def page(a, b, budget):
            params = {"start": a.strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "end": b.strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "interval": interval, "limit": self.page, "order": "asc"}
            rows, cursor = [], None
            while self.calls < budget:
                p = {"cursor": cursor} if cursor else params
                j = self._get(url, p)
                data = j.get("data", [])
                if not isinstance(data, list):
                    raise RuntimeError("sifting REST returned invalid data")
                rows.extend(data)
                cursor = (j.get("meta") or {}).get("next_cursor")
                if not cursor:
                    break
            return rows

        rows = page(start, end, fetch_limit)
        # A multi-hour range returning one bar is an upstream short page, not
        # a valid backfill. Retry in daily windows instead of silently starting
        # the bot with one candle (the failure seen in the Render log).
        if len(rows) <= 1 and end - start > pd.Timedelta(hours=6) and self.calls < fetch_limit:
            rows = []
            cursor = start
            while cursor < end and self.calls < fetch_limit:
                nxt = min(cursor + pd.Timedelta(days=1), end)
                rows.extend(page(cursor, nxt, fetch_limit))
                cursor = nxt
        if not rows:
            return pd.DataFrame(columns=OHLCV, index=pd.DatetimeIndex([], tz="UTC"))
        df = pd.DataFrame(rows)
        df.index = pd.to_datetime(df["t"], unit="ms", utc=True)
        df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})[OHLCV]
        df = df.astype(float).sort_index()
        return df[~df.index.duplicated(keep="last")]

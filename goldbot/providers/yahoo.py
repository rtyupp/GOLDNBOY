"""Free Yahoo Finance historical feed (no API key, no account).

Chosen as the default history source because the public Dukascopy tick feed is
frequently unreachable / rate-limited (HTTP 429/503) from cloud hosts, which left
the bot with no candles and therefore no analysis.

Yahoo exposes intraday bars through the public chart endpoint. 1m bars are only
served ~4 weeks back, so the request is split into small chunks and the pieces are
merged; the missing tail (most recent bars) is what the app keeps refreshing.
Spot gold carries no real volume, so volume is mostly zero here — exactly the case
the indicator layer already handles (it substitutes 1.0).
"""
from __future__ import annotations

import logging
import time

import pandas as pd
import requests

from goldbot.core.timeframes import OHLCV, resample_ohlcv
from goldbot.providers.base import HistoryProvider

log = logging.getLogger("yahoo")

# interval -> (yahoo granularity, max days Yahoo keeps for it, chunk size in days)
_INTERVALS = {
    "1m": ("1m", 28, 5),
    "5m": ("5m", 58, 20),
    "15m": ("15m", 58, 20),
    "30m": ("30m", 58, 20),
    "1h": ("60m", 720, 120),
    "1d": ("1d", 3650, 365),
}


class YahooHistory(HistoryProvider):
    def __init__(self, cfg: dict, symbol: str = "XAUUSD", session: requests.Session | None = None):
        self.base = cfg.get("url", "https://query1.finance.yahoo.com/v8/finance/chart").rstrip("/")
        # XAUUSD (spot) is not on Yahoo; GC=F (COMEX gold futures) tracks it closely.
        default_sym = "GC=F" if symbol.replace("/", "").upper() in ("XAUUSD", "GOLD") else symbol
        self.symbol = cfg.get("instrument", cfg.get("yahoo_symbol", default_sym))
        self.timeout = float(cfg.get("timeout_sec", 20))
        self.retries = max(1, int(cfg.get("retries", 3)))
        self.pause = float(cfg.get("pause_sec", 0.0))
        self.s = session or requests.Session()
        ua = str(cfg.get("user_agent", "Mozilla/5.0 (compatible; goldbot/1.0)"))
        current = self.s.headers.get("User-Agent", "")
        if not current or current.lower().startswith("python-requests"):
            self.s.headers["User-Agent"] = ua
        self.stats = {"ok": 0, "errors": 0}
        self.last_error: str | None = None

    # ------------------------------------------------------------------ HTTP
    def _get(self, params: dict) -> dict:
        err = None
        for attempt in range(self.retries):
            if self.pause:
                time.sleep(self.pause)
            try:
                r = self.s.get(self.base + "/" + self.symbol, params=params, timeout=self.timeout)
            except requests.RequestException as e:
                err = f"{type(e).__name__}"
                time.sleep(0.5 * (attempt + 1))
                continue
            if r.status_code == 200:
                try:
                    payload = r.json()
                except ValueError as e:
                    err = f"bad json: {type(e).__name__}"
                    continue
                return payload
            err = f"HTTP {r.status_code}"
            if r.status_code in (429, 503):
                time.sleep(2.0 * (attempt + 1))
            else:
                time.sleep(0.6 * (attempt + 1))
        raise RuntimeError(f"Yahoo غير متاح ({err})")

    @staticmethod
    def _parse(payload: dict, interval: str) -> pd.DataFrame:
        chart = payload.get("chart") or {}
        err = chart.get("error")
        if err:
            # "Unprocessable Entity" = the requested window is older than Yahoo keeps
            raise LookupError(str(err.get("code")))
        result = chart.get("result")
        if not result:
            return pd.DataFrame(columns=OHLCV, index=pd.DatetimeIndex([], tz="UTC"))
        res = result[0]
        ts = res.get("timestamp") or []
        quote = (res.get("indicators", {}).get("quote") or [{}])[0]
        if not ts or not quote.get("close"):
            return pd.DataFrame(columns=OHLCV, index=pd.DatetimeIndex([], tz="UTC"))
        idx = pd.to_datetime(pd.Series(ts), unit="s", utc=True)
        df = pd.DataFrame({
            "open": quote.get("open"),
            "high": quote.get("high"),
            "low": quote.get("low"),
            "close": quote.get("close"),
            "volume": quote.get("volume"),
        }, index=idx)
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0)
        df = df.dropna(subset=["open", "high", "low", "close"])
        df = df.astype(float)
        return df[OHLCV]

    # ------------------------------------------------------------------ fetch
    def max_history_days(self, interval: str):
        return _INTERVALS.get(interval, _INTERVALS["1m"])[1]

    def fetch(self, interval: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        start = pd.Timestamp(start)
        end = pd.Timestamp(end)
        start = start.tz_localize("UTC") if start.tzinfo is None else start.tz_convert("UTC")
        end = end.tz_localize("UTC") if end.tzinfo is None else end.tz_convert("UTC")
        granularity, max_days, chunk_days = _INTERVALS.get(interval, _INTERVALS["1m"])

        now = pd.Timestamp.now(tz="UTC")
        # Yahoo refuses windows older than its intraday retention; clamp and fetch the rest.
        floor = now - pd.Timedelta(days=max_days)
        lo = max(start.floor("min"), floor)
        hi = min(end, now)
        self.stats = {"ok": 0, "errors": 0}
        if lo >= hi:
            return pd.DataFrame(columns=OHLCV, index=pd.DatetimeIndex([], tz="UTC"))

        chunks = []
        cur = lo
        while cur < hi:
            nxt = min(cur + pd.Timedelta(days=chunk_days), hi)
            chunks.append((cur, nxt))
            cur = nxt

        frames, blocked = [], 0
        for c_start, c_end in chunks:
            params = {"period1": int(c_start.timestamp()), "period2": int(c_end.timestamp()),
                      "interval": granularity, "includePrePost": "false", "events": "div,splits"}
            try:
                df = self._parse(self._get(params), interval)
            except LookupError:
                continue                       # window too old for Yahoo: skip, keep what we have
            except Exception as e:             # network/HTTP: remember but keep going
                blocked += 1
                self.last_error = str(e)
                continue
            self.stats["ok"] += 1
            if not df.empty:
                frames.append(df)

        if not frames:
            if blocked:
                raise RuntimeError(f"Yahoo غير متاح ({blocked} فشل، آخر خطأ: {self.last_error})")
            return pd.DataFrame(columns=OHLCV, index=pd.DatetimeIndex([], tz="UTC"))

        out = pd.concat(frames).sort_index()
        out = out[~out.index.duplicated(keep="last")]
        out = out[(out.index >= start) & (out.index < end)]
        if out.empty:
            return pd.DataFrame(columns=OHLCV, index=pd.DatetimeIndex([], tz="UTC"))
        if interval != "1m":
            tf = {"5m": "5m", "15m": "15m", "30m": "30m", "1h": "1H", "1d": "1D"}.get(interval)
            if tf:
                out = resample_ohlcv(out, tf)[OHLCV]
        return out.astype(float)

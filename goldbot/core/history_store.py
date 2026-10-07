"""Disk cache for history so we spend few REST calls (free plan = 10k calls/month)."""
from __future__ import annotations
import logging, os
import pandas as pd
from goldbot.core.timeframes import OHLCV, normalize_ohlcv

log = logging.getLogger("history")
_STEP = {"1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min", "1h": "1h", "1d": "1D"}


def load_cached(provider, symbol: str, interval: str, days: int, cache_dir: str,
                now: pd.Timestamp | None = None) -> pd.DataFrame:
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"{symbol}_{interval}.csv.gz")
    now = (now or pd.Timestamp.now(tz="UTC")).floor("1min")
    want_start = now - pd.Timedelta(days=days)
    cached = pd.DataFrame(columns=OHLCV, index=pd.DatetimeIndex([], tz="UTC"))
    if os.path.exists(path):
        try:
            c = pd.read_csv(path, index_col=0, parse_dates=True)
            cached = normalize_ohlcv(c)
        except Exception as e:
            log.warning("ملف الكاش تالف (%s) ← إعادة التحميل", e)
    start = want_start
    if not cached.empty and cached.index[0] <= want_start + pd.Timedelta(days=3):
        start = cached.index[-1]            # only download the missing tail
    fresh = provider.fetch(interval, start, now)
    if not fresh.empty:
        # drop the still-forming bar so we never cache a partial candle
        step = pd.Timedelta(_STEP[interval])
        fresh = fresh[fresh.index + step <= now]
    df = pd.concat([cached, fresh]) if not fresh.empty else cached
    if not df.empty:
        df = normalize_ohlcv(df)
        df.to_csv(path, compression="gzip")
    return df[df.index >= want_start]

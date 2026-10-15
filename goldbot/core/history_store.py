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
    try:
        fresh = provider.fetch(interval, start, now)
    except Exception as e:
        if cached.empty:
            raise
        log.warning("تعذّر تحديث الذيل (%s) ← استخدام الكاش الموجود", e)
        fresh = cached.iloc[0:0]
    if not fresh.empty:
        # drop the still-forming bar so we never cache a partial candle
        step = pd.Timedelta(_STEP[interval])
        fresh = fresh[fresh.index + step <= now]
    df = pd.concat([cached, fresh]) if not fresh.empty else cached
    if not df.empty:
        df = normalize_ohlcv(df)
        df.to_csv(path, compression="gzip")
    return df[df.index >= want_start]


def load_backtest_base(provider, symbol: str, cfg, days: int | None = None,
                       now: pd.Timestamp | None = None) -> pd.DataFrame:
    """Base series for backtests/bootstrap.

    Free Yahoo keeps ~58 days of 5m but only ~28 days of 1m, so a 1m-only base caps the historical
    sample. This stitches native 5m bars for the deeper span in front of native 1m bars for the
    recent span: the probability sample roughly doubles while the recent window keeps full 1m
    precision (trade simulation and the 1m confirmation flag). Falls back to 1m-only when the source
    cannot serve deeper 5m (or when `backtest.extend_with_native_5m` is off)."""
    cache_dir = cfg.get("data.cache_dir", "data/cache")
    days = int(days or cfg.get("backtest.history_days", 60))
    now = (now or pd.Timestamp.now(tz="UTC")).floor("1min")
    cap1 = provider.max_history_days("1m")
    base1 = load_cached(provider, symbol, "1m", min(days, cap1) if cap1 else days, cache_dir, now)
    if not cfg.get("backtest.extend_with_native_5m", True) or base1.empty:
        return base1
    cap5 = provider.max_history_days("5m")
    if not cap5 or (cap1 and cap5 <= cap1) or days <= (cap1 or 0):
        return base1
    older = load_cached(provider, symbol, "5m", days, cache_dir, now)
    older = older[older.index < base1.index[0]]
    if older.empty:
        return base1
    return normalize_ohlcv(pd.concat([older, base1]))

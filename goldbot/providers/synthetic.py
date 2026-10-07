"""Synthetic gold-like data. FOR TESTS / DEMO ONLY - never used for real signals."""
from __future__ import annotations
import numpy as np
import pandas as pd
from goldbot.providers.base import HistoryProvider
from goldbot.core.timeframes import OHLCV


def make_synthetic_1m(days: int = 20, seed: int = 7, start_price: float = 2650.0,
                      end: pd.Timestamp | None = None, drift_scale: float = 1.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    end = end or pd.Timestamp("2026-06-12 00:00", tz="UTC")
    idx = pd.date_range(end=end, periods=days * 1440, freq="1min", tz="UTC")
    idx = idx[idx.dayofweek < 5]                      # no weekends
    n = len(idx)
    hours = idx.hour.values
    vol = np.where((hours >= 7) & (hours < 17), 0.75, np.where(hours < 7, 0.40, 0.55))
    # slowly switching drift regimes -> trends / ranges
    reg = np.repeat(rng.choice([-1, 0, 1], size=n // 400 + 1, p=[0.3, 0.4, 0.3]), 400)[:n]
    drift = reg * 0.09 * drift_scale
    ret = drift + rng.standard_normal(n) * vol
    close = start_price + np.cumsum(ret)
    open_ = np.r_[close[0], close[:-1]]
    wick = np.abs(rng.standard_normal((n, 2))) * vol[:, None] * 0.8
    high = np.maximum(open_, close) + wick[:, 0]
    low = np.minimum(open_, close) - wick[:, 1]
    volume = (rng.poisson(30, n) * (1 + 1.5 * (vol > 0.7))).astype(float)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx)


class SyntheticHistory(HistoryProvider):
    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or {}

    def fetch(self, interval, start, end):
        df = make_synthetic_1m(days=int(self.cfg.get("days", 30)), seed=int(self.cfg.get("seed", 7)))
        return df[(df.index >= start) & (df.index <= end)]

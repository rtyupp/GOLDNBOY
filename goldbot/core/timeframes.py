from __future__ import annotations
import pandas as pd

TF_RULES = {"1m": "1min", "3m": "3min", "5m": "5min", "15m": "15min", "30m": "30min",
            "1H": "1h", "4H": "4h", "1D": "1D"}
TF_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1H": 60, "4H": 240, "1D": 1440}
ALL_TFS = ["1m", "3m", "5m", "15m", "30m", "1H", "4H", "1D"]
OHLCV = ["open", "high", "low", "close", "volume"]


def normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    df = df[OHLCV].copy()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df.astype({"open": float, "high": float, "low": float, "close": float, "volume": float})


def resample_ohlcv(base: pd.DataFrame, tf: str) -> pd.DataFrame:
    if tf == "1m":
        r = base.copy()
    else:
        r = base.resample(TF_RULES[tf], label="left", closed="left").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        r = r.dropna(subset=["open"])
    r["close_time"] = r.index + pd.Timedelta(minutes=TF_MINUTES[tf])
    return r


def closed_only(frame: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    """Only candles that are fully closed at `as_of` (never the forming one => no future data)."""
    n = frame["close_time"].searchsorted(as_of, side="right")
    return frame.iloc[:n]

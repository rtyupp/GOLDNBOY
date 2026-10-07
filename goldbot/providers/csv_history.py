"""Any free source: drop a CSV with columns time,open,high,low,close[,volume] (UTC)."""
from __future__ import annotations
import pandas as pd
from goldbot.providers.base import HistoryProvider
from goldbot.core.timeframes import OHLCV, normalize_ohlcv, resample_ohlcv


class CsvHistory(HistoryProvider):
    def __init__(self, cfg: dict):
        self.path = cfg.get("csv_path", "data/xauusd_1m.csv")

    def fetch(self, interval: str, start, end) -> pd.DataFrame:
        df = pd.read_csv(self.path)
        tcol = next(c for c in df.columns if c.lower() in ("time", "timestamp", "datetime", "date"))
        df.index = pd.to_datetime(df[tcol], utc=True)
        df.columns = [c.lower() for c in df.columns]
        if "volume" not in df.columns:
            df["volume"] = 0.0
        df = normalize_ohlcv(df)
        df = df[(df.index >= start) & (df.index <= end)]
        tf = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1H", "1d": "1D"}.get(interval, "1m")
        if tf != "1m":
            df = resample_ohlcv(df, tf)[OHLCV]
        return df

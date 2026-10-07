"""Base 1m store = historical (REST/cache) + live-built candles. All timeframes derive from it.

LIVE PRICE != HISTORICAL DATA: this store never blocks the tick feed; if history fails the
live feed keeps running and the analysis simply reports DATA/CANDLES not OK.
"""
from __future__ import annotations
import threading, logging
from typing import Dict, Optional
import pandas as pd
from goldbot.core.candle_builder import Candle
from goldbot.core.timeframes import normalize_ohlcv, resample_ohlcv, closed_only, ALL_TFS, OHLCV

log = logging.getLogger("market_data")


class MarketData:
    def __init__(self, max_rows: int = 120_000):
        self.base = pd.DataFrame({c: pd.Series(dtype="float64") for c in OHLCV}, index=pd.DatetimeIndex([], tz="UTC"))
        self.max_rows = max_rows
        self.live_candles = 0
        self.history_rows = 0
        self.history_error: Optional[str] = None
        self.spreads: Dict[int, float] = {}
        self._lock = threading.Lock()
        self._pending: list = []

    def load_history(self, df: pd.DataFrame):
        df = normalize_ohlcv(df)
        with self._lock:
            live = self.base.iloc[-self.live_candles:] if self.live_candles else self.base.iloc[0:0]
            self.base = pd.concat([df, live]).astype("float64")
            self.base = self.base[~self.base.index.duplicated(keep="last")].sort_index()
            self.history_rows = len(df)
            self._trim()

    def add_closed(self, c: Candle):
        """Live candle overrides REST candle of the same minute (built from our own ticks)."""
        ts = pd.Timestamp(c.t0, unit="ms", tz="UTC")
        row = pd.DataFrame({"open": [c.open], "high": [c.high], "low": [c.low],
                            "close": [c.close], "volume": [float(c.volume)]}, index=[ts])
        with self._lock:
            if ts in self.base.index:
                self.base = self.base.drop(index=ts)
            self.base = pd.concat([self.base, row]).astype("float64").sort_index()
            self.live_candles += 1
            if c.avg_spread is not None:
                self.spreads[c.t0] = c.avg_spread
                if len(self.spreads) > 2000:
                    for k in sorted(self.spreads)[:500]:
                        self.spreads.pop(k, None)
            self._trim()

    def _trim(self):
        if len(self.base) > self.max_rows:
            self.base = self.base.iloc[-self.max_rows:]

    def last_base_close(self) -> Optional[pd.Timestamp]:
        if self.base.empty:
            return None
        return self.base.index[-1] + pd.Timedelta(minutes=1)

    def frames(self, as_of: pd.Timestamp, tfs=ALL_TFS, tail: Optional[int] = None) -> Dict[str, pd.DataFrame]:
        with self._lock:
            base = self.base.copy()
        base = base[base.index < as_of]          # nothing at/after as_of
        out = {}
        for tf in tfs:
            f = closed_only(resample_ohlcv(base, tf), as_of)
            out[tf] = f.iloc[-tail:] if tail else f
        return out

    def summary(self) -> str:
        if self.base.empty:
            return "empty"
        return f"{len(self.base)} x 1m  [{self.base.index[0]:%Y-%m-%d %H:%M} .. {self.base.index[-1]:%Y-%m-%d %H:%M}] live={self.live_candles}"

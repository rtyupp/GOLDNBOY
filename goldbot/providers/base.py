from __future__ import annotations
import pandas as pd


class LiveProvider:
    """Live price source. Must push goldbot.models.Tick objects to the callback."""
    async def run(self) -> None: ...
    def stop(self) -> None: ...


class HistoryProvider:
    """Historical OHLCV source (synchronous; the app runs it in a worker thread)."""
    def fetch(self, interval: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        """Return df indexed by UTC bar-open time with open/high/low/close/volume."""
        raise NotImplementedError

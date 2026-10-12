"""Historical data provider backed by the vendored dukascopy-node engine."""
from __future__ import annotations
import json
import logging
import os
import subprocess
from pathlib import Path
import pandas as pd
from goldbot.core.timeframes import OHLCV, resample_ohlcv
from goldbot.providers.base import HistoryProvider

log = logging.getLogger("dukascopy_node")


class DukascopyNodeHistory(HistoryProvider):
    """Download XAUUSD history through dukascopy-node instead of parsing .bi5 files in Python."""
    def __init__(self, cfg: dict, symbol: str = "XAUUSD"):
        self.symbol = str(cfg.get("instrument", symbol)).replace("/", "").lower()
        self.timeout = float(cfg.get("timeout_sec", 180))
        self.cache_dir = str(cfg.get("cache_dir", "data/cache/dukascopy-node"))
        self.bridge = Path(__file__).with_name("dukascopy_node_bridge.js")
        self.progress = (0, 0)
        self.last_error = None

    def fetch(self, interval: str, start, end) -> pd.DataFrame:
        start = pd.Timestamp(start)
        end = pd.Timestamp(end)
        start = start.tz_localize("UTC") if start.tzinfo is None else start.tz_convert("UTC")
        end = end.tz_localize("UTC") if end.tzinfo is None else end.tz_convert("UTC")
        tf = {"1m": "m1", "5m": "m5", "15m": "m15", "30m": "m30",
              "1h": "h1", "1H": "h1", "4H": "h4", "1d": "d1", "1D": "d1"}.get(interval)
        if tf is None:
            raise ValueError(f"unsupported dukascopy-node timeframe: {interval}")
        payload = {"instrument": self.symbol, "from": start.isoformat(), "to": end.isoformat(),
                   "timeframe": tf, "cache": self.cache_dir}
        self.progress = (0, 1)
        env = os.environ.copy()
        env["DUKASCOPY_NODE_CACHE"] = self.cache_dir
        try:
            r = subprocess.run(["node", str(self.bridge)], input=json.dumps(payload), text=True,
                               capture_output=True, timeout=self.timeout, env=env, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            self.last_error = f"{type(e).__name__}: {e}"
            raise RuntimeError(f"تعذر تشغيل محرك التاريخ الجديد: {self.last_error}") from e
        if r.returncode != 0:
            detail = (r.stderr or r.stdout or "unknown node error").strip()[-500:]
            self.last_error = detail
            raise RuntimeError(f"محرك dukascopy-node فشل: {detail}")
        try:
            rows = json.loads(r.stdout or "[]")
        except json.JSONDecodeError as e:
            self.last_error = (r.stdout or r.stderr)[-500:]
            raise RuntimeError("محرك dukascopy-node أعاد بيانات غير صالحة") from e
        if not rows:
            return pd.DataFrame(columns=OHLCV, index=pd.DatetimeIndex([], tz="UTC"))
        df = pd.DataFrame(rows)
        required = ["timestamp", "open", "high", "low", "close"]
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise RuntimeError(f"بيانات dukascopy-node ناقصة: {missing}")
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        if "volume" not in df.columns:
            df["volume"] = 0.0
        df = df.set_index("timestamp")[OHLCV].sort_index()
        df = df[~df.index.duplicated(keep="last")]
        df = df[(df.index >= start) & (df.index < end)].astype(float)
        self.progress = (1, 1)
        return df

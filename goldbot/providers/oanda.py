"""OANDA Practice provider: official M1/M5 candles and streaming prices.

Requires a free OANDA Practice account, account id, and personal access token.
This is broker-specific XAU_USD/CFD pricing, not a universal gold reference.
"""
from __future__ import annotations
import asyncio
import json
import logging
import random
import threading
import time
from typing import Callable, Optional

import pandas as pd
import requests

from goldbot.models import Tick
from goldbot.providers.base import HistoryProvider, LiveProvider
from goldbot.core.timeframes import OHLCV

log = logging.getLogger("oanda")


def _price(v):
    try:
        x = float(v)
        return x if x > 0 else None
    except (TypeError, ValueError):
        return None


def _ts(v) -> int | None:
    try:
        return int(pd.Timestamp(v).timestamp() * 1000)
    except Exception:
        return None


class OandaHistory(HistoryProvider):
    """Fetch complete OANDA candles in bounded pages (max 5,000 each)."""

    GRANULARITY = {"1m": "M1", "5m": "M5", "15m": "M15", "30m": "M30", "1h": "H1", "1d": "D"}

    def __init__(self, cfg: dict, token: str, account_id: str, instrument: str = "XAU_USD",
                 session: requests.Session | None = None):
        self.base = cfg.get("rest_url", "https://api-fxpractice.oanda.com").rstrip("/")
        self.token = token
        self.account_id = account_id
        self.instrument = instrument
        self.s = session or requests.Session()
        self.page = min(5000, int(cfg.get("oanda_page_limit", 5000)))
        self.max_calls = int(cfg.get("oanda_max_calls", 80))
        self.calls = 0

    def fetch(self, interval: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        if interval not in self.GRANULARITY:
            raise ValueError(f"OANDA interval {interval} unsupported")
        start, end = pd.Timestamp(start), pd.Timestamp(end)
        start = start.tz_localize("UTC") if start.tzinfo is None else start.tz_convert("UTC")
        end = end.tz_localize("UTC") if end.tzinfo is None else end.tz_convert("UTC")
        if end <= start:
            return pd.DataFrame(columns=OHLCV, index=pd.DatetimeIndex([], tz="UTC"))
        step = pd.Timedelta({"1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min", "1h": "1h", "1d": "1d"}[interval])
        url = f"{self.base}/v3/instruments/{self.instrument}/candles"
        headers = {"Authorization": f"Bearer {self.token}", "Accept-Datetime-Format": "RFC3339"}
        rows, cursor, n = [], start, 0
        while cursor < end and n < self.max_calls:
            params = {"from": cursor.isoformat().replace("+00:00", "Z"),
                      "to": end.isoformat().replace("+00:00", "Z"),
                      "granularity": self.GRANULARITY[interval], "price": "M", "count": self.page}
            r = self.s.get(url, params=params, headers=headers, timeout=30)
            self.calls += 1
            n += 1
            if r.status_code >= 400:
                raise RuntimeError(f"OANDA history {r.status_code}: {r.text[:300]}")
            payload = r.json()
            candles = payload.get("candles") or []
            if not candles:
                break
            last = None
            for c in candles:
                if not c.get("complete", False):
                    continue
                mid = c.get("mid") or {}
                t = pd.Timestamp(c.get("time"))
                t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
                row = {"open": _price(mid.get("o")), "high": _price(mid.get("h")),
                       "low": _price(mid.get("l")), "close": _price(mid.get("c")),
                       "volume": float(c.get("volume") or 0)}
                if all(row[k] is not None for k in ("open", "high", "low", "close")):
                    rows.append((t, row))
                last = t
            if last is None or last < cursor:
                break
            cursor = last + step
            if len(candles) < self.page:
                break
        if not rows:
            return pd.DataFrame(columns=OHLCV, index=pd.DatetimeIndex([], tz="UTC"))
        df = pd.DataFrame([r for _, r in rows], index=pd.DatetimeIndex([t for t, _ in rows]))
        df = df[~df.index.duplicated(keep="last")].sort_index()
        return df[OHLCV].astype(float)


class OandaStream(LiveProvider):
    """OANDA pricing stream converted to goldbot Tick objects."""

    def __init__(self, cfg: dict, token: str, account_id: str, instrument: str,
                 on_tick: Callable[[Tick], None], on_state: Callable[[bool], None] | None = None,
                 session: requests.Session | None = None, clock: Callable[[], float] = time.time):
        self.base = cfg.get("stream_url", "https://stream-fxpractice.oanda.com").rstrip("/")
        self.token, self.account_id, self.instrument = token, account_id, instrument
        self.on_tick = on_tick
        self.on_state = on_state or (lambda ok: None)
        self.s = session or requests.Session()
        self.clock = clock
        self._stop = threading.Event()
        self.last_error: Optional[str] = None
        self.reconnect_min = float(cfg.get("oanda_reconnect_min_sec", 2))
        self.reconnect_max = float(cfg.get("oanda_reconnect_max_sec", 60))

    def stop(self):
        self._stop.set()

    def _parse(self, msg: dict, recv_ms: int) -> Tick | None:
        if msg.get("type") != "PRICE" or msg.get("instrument") != self.instrument:
            return None
        bids, asks = msg.get("bids") or [], msg.get("asks") or []
        bid = _price((bids[0] if bids else {}).get("price")) or _price(msg.get("closeoutBid"))
        ask = _price((asks[0] if asks else {}).get("price")) or _price(msg.get("closeoutAsk"))
        if bid is None or ask is None or ask < bid:
            return None
        ts = _ts(msg.get("time"))
        if ts is None:
            return None
        return Tick(ts_ms=ts, bid=bid, ask=ask, last=(bid + ask) / 2, recv_ms=recv_ms)

    def _session(self):
        url = f"{self.base}/v3/accounts/{self.account_id}/pricing/stream"
        headers = {"Authorization": f"Bearer {self.token}", "Accept-Datetime-Format": "RFC3339"}
        with self.s.get(url, params={"instruments": self.instrument}, headers=headers,
                        stream=True, timeout=(15, None)) as r:
            if r.status_code >= 400:
                raise RuntimeError(f"OANDA stream {r.status_code}: {r.text[:300]}")
            self.on_state(True)
            for raw in r.iter_lines(decode_unicode=True):
                if self._stop.is_set():
                    break
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                tick = self._parse(msg, int(self.clock() * 1000))
                if tick:
                    self.on_tick(tick)
        self.on_state(False)

    async def run(self) -> None:
        backoff = self.reconnect_min
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self._session)
                backoff = self.reconnect_min
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                self.on_state(False)
                log.warning("OANDA stream: %s", self.last_error)
            if self._stop.is_set():
                break
            await asyncio.sleep(min(self.reconnect_max, backoff) * (0.8 + 0.4 * random.random()))
            backoff = min(self.reconnect_max, backoff * 2)

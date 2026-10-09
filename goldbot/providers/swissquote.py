"""Public Swissquote BBO polling for XAU/USD.

The endpoint is public and does not require an API key. It supplies live bid/ask
snapshots; the bot converts snapshots to Tick objects and builds M1/M5 candles
locally. It is a market-data reference, not an execution-price guarantee.
"""
from __future__ import annotations
import asyncio
import logging
import random
import statistics
import threading
import time
from typing import Callable, Optional

import requests

from goldbot.models import Tick
from goldbot.providers.base import LiveProvider

log = logging.getLogger("swissquote")


class SwissquotePublic(LiveProvider):
    def __init__(self, cfg: dict, symbol: str, on_tick: Callable[[Tick], None],
                 on_state: Callable[[bool], None] | None = None,
                 session: requests.Session | None = None,
                 clock: Callable[[], float] = time.time):
        self.url = cfg.get("url", "https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/XAU/USD")
        self.interval = max(3.0, float(cfg.get("poll_sec", 5)))
        self.reconnect_min = float(cfg.get("reconnect_min_sec", 2))
        self.reconnect_max = float(cfg.get("reconnect_max_sec", 60))
        self.symbol = symbol.upper()
        self.on_tick = on_tick
        self.on_state = on_state or (lambda ok: None)
        self.s = session or requests.Session()
        self.clock = clock
        self._stop = threading.Event()
        self.last_error: Optional[str] = None

    def stop(self):
        self._stop.set()

    @staticmethod
    def parse(payload, recv_ms: int) -> Tick | None:
        if not isinstance(payload, list):
            return None
        mids, spreads, timestamps = [], [], []
        for venue in payload:
            try:
                ts = int(venue.get("ts"))
            except (TypeError, ValueError):
                ts = 0
            for q in venue.get("spreadProfilePrices") or []:
                try:
                    bid, ask = float(q.get("bid")), float(q.get("ask"))
                except (TypeError, ValueError):
                    continue
                if bid > 0 and ask >= bid:
                    mids.append((bid + ask) / 2.0)
                    spreads.append(ask - bid)
                    if ts > 0:
                        timestamps.append(ts)
        if not mids:
            return None
        mid = statistics.median(mids)
        spread = statistics.median(spreads)
        ts = int(statistics.median(timestamps)) if timestamps else recv_ms
        return Tick(ts_ms=ts, bid=mid - spread / 2.0, ask=mid + spread / 2.0,
                    last=mid, recv_ms=recv_ms)

    def _once(self):
        r = self.s.get(self.url, timeout=10, headers={"Accept": "application/json"})
        if r.status_code >= 400:
            raise RuntimeError(f"Swissquote HTTP {r.status_code}: {r.text[:200]}")
        t = self.parse(r.json(), int(self.clock() * 1000))
        if t is None:
            raise RuntimeError("Swissquote returned no valid XAU/USD quote")
        self.on_tick(t)
        self.on_state(True)

    async def run(self) -> None:
        backoff = self.reconnect_min
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self._once)
                backoff = self.reconnect_min
                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                self.on_state(False)
                log.warning("Swissquote live feed: %s", self.last_error)
                await asyncio.sleep(min(self.reconnect_max, backoff) * (0.8 + 0.4 * random.random()))
                backoff = min(self.reconnect_max, backoff * 2)

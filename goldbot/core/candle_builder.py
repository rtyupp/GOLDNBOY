"""Builds 1-minute candles from live ticks (mid price, volume = tick count)."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable, List, Optional
from goldbot.models import Tick


@dataclass
class Candle:
    t0: int          # bucket open, epoch ms
    open: float
    high: float
    low: float
    close: float
    volume: int      # tick count (spot gold has no real volume)
    spread_sum: float = 0.0
    spread_n: int = 0

    @property
    def avg_spread(self) -> Optional[float]:
        return self.spread_sum / self.spread_n if self.spread_n else None


class CandleBuilder:
    def __init__(self, on_close: Callable[[Candle], None] | None = None):
        self.cur: Optional[Candle] = None
        self.on_close = on_close
        self.closed_count = 0

    def _emit(self, c: Candle):
        self.closed_count += 1
        if self.on_close:
            self.on_close(c)

    def on_tick(self, t: Tick):
        px = t.mid
        b = (t.ts_ms // 60000) * 60000
        c = self.cur
        if c is not None and b < c.t0:
            return                      # late tick for an already closed minute: ignore (no rewriting past)
        if c is None or b > c.t0:
            if c is not None:
                self._emit(c)
            c = self.cur = Candle(b, px, px, px, px, 0)
        c.high = max(c.high, px)
        c.low = min(c.low, px)
        c.close = px
        c.volume += 1
        if t.spread is not None:
            c.spread_sum += t.spread
            c.spread_n += 1

    def flush(self, now_ms: int, grace_ms: int = 1500):
        """Close the forming candle when its minute is over even if no new tick arrived."""
        c = self.cur
        if c is not None and now_ms >= c.t0 + 60000 + grace_ms:
            self.cur = None
            self._emit(c)

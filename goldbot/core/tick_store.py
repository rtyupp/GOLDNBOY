"""Last-tick store with freshness checks. Live price is independent from history/analysis."""
from __future__ import annotations
import time, threading
from collections import deque
from typing import Callable, List, Optional, Tuple
from goldbot.models import Tick


class TickStore:
    def __init__(self, max_age_sec: float = 20.0, clock: Callable[[], float] = time.time, keep: int = 5000):
        self.max_age_sec = max_age_sec
        self.clock = clock
        self.last: Optional[Tick] = None
        self.recent: deque = deque(maxlen=keep)
        self.count = 0
        self.connected = False
        self.last_connect_ms: Optional[int] = None
        self._lock = threading.Lock()
        self._listeners: List[Callable[[Tick], None]] = []

    def add_listener(self, fn: Callable[[Tick], None]):
        self._listeners.append(fn)

    def on_tick(self, t: Tick):
        with self._lock:
            # ignore ticks older than the newest one (out-of-order / replayed snapshot)
            if self.last is not None and t.ts_ms < self.last.ts_ms:
                return
            self.last = t
            self.recent.append(t)
            self.count += 1
        for fn in self._listeners:
            try:
                fn(t)
            except Exception:  # listener errors must never break the feed
                import logging
                logging.getLogger("tick_store").exception("tick listener failed")

    def set_connected(self, ok: bool):
        self.connected = ok
        if ok:
            self.last_connect_ms = int(self.clock() * 1000)

    def age_sec(self) -> Optional[float]:
        """Age of newest tick: the worst (largest) of source-timestamp age and local receive age."""
        t = self.last
        if t is None:
            return None
        now_ms = self.clock() * 1000
        return max(0.0, max(now_ms - t.ts_ms, now_ms - t.recv_ms) / 1000.0)

    def check_fresh(self) -> Tuple[bool, str]:
        if self.last is None:
            return False, "NO_TICK_YET"
        if not self.connected:
            return False, "WS_DISCONNECTED"
        age = self.age_sec()
        if age is None or age > self.max_age_sec:
            return False, f"STALE_DATA({age:.1f}s>{self.max_age_sec:.0f}s)"
        return True, "OK"

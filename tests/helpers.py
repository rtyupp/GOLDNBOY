import asyncio, json
import numpy as np, pandas as pd
from goldbot.config import Cfg, load_config


def cfg(**over):
    c = load_config("config/config.yaml")
    for k, v in over.items():
        c.set(k.replace("__", "."), v)
    return c


def ohlc(rows, start="2026-06-01 00:00", freq="5min"):
    idx = pd.date_range(start, periods=len(rows), freq=freq, tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx).assign(volume=10.0)


def series_from_closes(closes, spread=0.2, **kw):
    rows = []
    prev = closes[0]
    for c in closes:
        rows.append((prev, max(prev, c) + spread, min(prev, c) - spread, c))
        prev = c
    return ohlc(rows, **kw)


class FakeWS:
    """Async fake websocket: feeds queued frames, records sent frames, can close."""
    def __init__(self, frames, close_after=True):
        self.q = asyncio.Queue()
        for f in frames:
            self.q.put_nowait(f if isinstance(f, str) else json.dumps(f))
        self.sent = []
        self.close_after = close_after

    async def send(self, s):
        self.sent.append(json.loads(s))

    async def recv(self):
        if self.q.empty() and self.close_after:
            raise ConnectionError("closed by fake")
        return await self.q.get()


class FakeConn:
    def __init__(self, ws):
        self.ws = ws

    async def __aenter__(self):
        return self.ws

    async def __aexit__(self, *a):
        return False


class FakeTG:
    def __init__(self):
        self.channel, self.photos, self.private = [], [], []
        self.admin_ids = {1}

    target = "private"

    def send_signal(self, t):
        self.channel.append(t)
        return True

    def send_signal_photo(self, png, cap):
        self.photos.append((len(png), cap))
        return True

    def send_message(self, chat, text):
        self.private.append((chat, text))
        return {"ok": 1}

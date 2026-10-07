"""SiftingIO WebSocket live feed (protocol: https://sifting.io/docs/websocket).

 - connect  wss://stream.sifting.io/ws/v1?key=KEY   -> server sends {"f":"ack","op":"auth",...}
 - subscribe {"op":"subscribe","product":"com","symbols":["XAUUSD"]}
 - ticks    {"f":"tick","class":"com","s":"XAUUSD","p":..,"b":..,"a":..,"t":epoch_ms}
 - ping     {"op":"ping"} -> {"f":"pong"}; server closes after 90s without CLIENT frames,
            so we ping every ping_interval_sec (default 30).
Free tier: 1 connection / 5 subscriptions -> we open exactly one connection and ONE symbol.
No polling is used while the socket is alive.
"""
from __future__ import annotations
import asyncio, json, logging, random, time
from typing import Callable, Optional
from goldbot.models import Tick
from goldbot.providers.base import LiveProvider

log = logging.getLogger("sifting_ws")


def _f(x) -> Optional[float]:
    try:
        v = float(x)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def parse_tick(msg: dict, symbol: str, recv_ms: int) -> Optional[Tick]:
    if msg.get("f") != "tick" or str(msg.get("s", "")).upper() != symbol.upper():
        return None
    ts = msg.get("t")
    if ts is None:
        return None
    bid, ask, last = _f(msg.get("b")), _f(msg.get("a")), _f(msg.get("p"))
    if bid is None and ask is None and last is None:
        return None
    if bid is not None and ask is not None and ask < bid:
        bid, ask = None, None            # crossed quote => unusable quote; keep only last price
    if (bid is None) != (ask is None):   # one-sided quote is not a usable quote
        bid, ask = None, None
    if bid is None and last is None:
        return None
    return Tick(ts_ms=int(ts), bid=bid, ask=ask, last=last, recv_ms=recv_ms)


class SiftingWSProvider(LiveProvider):
    def __init__(self, cfg: dict, api_key: str, symbol: str, on_tick: Callable[[Tick], None],
                 on_state: Callable[[bool], None] | None = None,
                 connect: Callable | None = None, clock: Callable[[], float] = time.time):
        self.url = cfg.get("ws_url", "wss://stream.sifting.io/ws/v1")
        self.product = cfg.get("product", "com")
        self.ping_interval = float(cfg.get("ping_interval_sec", 30))
        self.silence_reconnect = float(cfg.get("silence_reconnect_sec", 120))
        self.bmin = float(cfg.get("reconnect_min_sec", 1))
        self.bmax = float(cfg.get("reconnect_max_sec", 60))
        self.api_key = api_key
        self.symbol = symbol.upper()
        self.on_tick = on_tick
        self.on_state = on_state or (lambda ok: None)
        self._connect = connect
        self.clock = clock
        self._stop = False
        self.connects = 0
        self.last_error: Optional[str] = None
        self.fatal_auth = False

    def stop(self):
        self._stop = True

    def _default_connect(self, url: str):
        import websockets  # lazy: only needed for the real feed
        return websockets.connect(url, ping_interval=20, ping_timeout=20, close_timeout=5, max_queue=1024)

    async def run(self):
        backoff = self.bmin
        while not self._stop:
            got_data = False
            try:
                url = f"{self.url}?key={self.api_key}"
                connect = self._connect or self._default_connect
                log.info("جارٍ الاتصال بـ %s (الرمز %s)", self.url, self.symbol)
                async with connect(url) as ws:
                    self.connects += 1
                    got_data = await self._session(ws)
                    if got_data:
                        backoff = self.bmin
            except asyncio.CancelledError:
                raise
            except Exception as e:  # network / protocol error
                self.last_error = f"{type(e).__name__}: {e}"
                log.warning("خطأ في الاتصال اللحظي: %s", self.last_error)
            finally:
                self.on_state(False)
            if self._stop:
                break
            delay = min(self.bmax, backoff) * (0.8 + 0.4 * random.random())
            if self.fatal_auth:
                delay = max(delay, 60.0)       # bad key: do not hammer the server
            log.info("إعادة الاتصال بعد %.1f ثانية", delay)
            await asyncio.sleep(delay)
            backoff = min(self.bmax, backoff * 2)

    async def _session(self, ws) -> bool:
        await ws.send(json.dumps({"op": "subscribe", "product": self.product, "symbols": [self.symbol]}))
        ping_task = asyncio.create_task(self._pinger(ws))
        got = False
        last_rx = self.clock()
        try:
            while not self._stop:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=self.silence_reconnect)
                except asyncio.TimeoutError:
                    log.warning("لا توجد رسائل منذ %.0f ثانية ← إعادة اتصال", self.silence_reconnect)
                    return got
                last_rx = self.clock()
                if self._handle(raw):
                    got = True
        except Exception as e:
            # websockets raises ConnectionClosed on recv; treat as normal disconnect
            self.last_error = f"{type(e).__name__}: {e}"
            log.warning("انتهت جلسة الاتصال: %s", self.last_error)
        finally:
            ping_task.cancel()
        return got

    async def _pinger(self, ws):
        while True:
            await asyncio.sleep(self.ping_interval)
            await ws.send(json.dumps({"op": "ping"}))

    def _handle(self, raw) -> bool:
        """Returns True if a usable tick was delivered."""
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            log.debug("non-json frame ignored")
            return False
        f = msg.get("f")
        if f == "tick":
            t = parse_tick(msg, self.symbol, int(self.clock() * 1000))
            if t is not None:
                self.on_state(True)
                self.on_tick(t)
                return True
            return False
        if f == "ack":
            if msg.get("op") == "auth":
                log.info("تم تسجيل الدخول: الباقة=%s الحد الأقصى للاتصالات=%s الاشتراكات=%s", msg.get("tier"), msg.get("max_conn"), msg.get("max_subs"))
                self.fatal_auth = False
            elif msg.get("op") == "subscribe":
                log.info("تم الاشتراك في %s", msg.get("symbols"))
        elif f == "error":
            code = msg.get("code")
            self.last_error = f"{code}: {msg.get('message')}"
            log.error("خطأ من الخادم: %s", self.last_error)
            if code in ("auth_failed", "auth_required"):
                self.fatal_auth = True
        return False

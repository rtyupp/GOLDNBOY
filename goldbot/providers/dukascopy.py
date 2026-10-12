"""Free Dukascopy historical tick feed, converted to OHLCV bars (no API key, no account).

Dukascopy publishes compressed hourly .bi5 files. IMPORTANT: in the URL the month is ZERO-BASED
(January = 00 ... December = 11). Missing hours (weekends, the still-open current hour, upstream
errors) are skipped; a missing hour is never treated as success by FallbackHistory.
"""
from __future__ import annotations
import lzma
import logging
import os
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests

from goldbot.core.timeframes import OHLCV, resample_ohlcv
from goldbot.providers.base import HistoryProvider

log = logging.getLogger("dukascopy")


class DukascopyHistory(HistoryProvider):
    def __init__(self, cfg: dict, symbol: str = "XAUUSD", session: requests.Session | None = None):
        self.base = cfg.get("url", "https://datafeed.dukascopy.com/datafeed").rstrip("/")
        # second attempt on the other scheme if the first one fails at the network level
        self.alt_base = self.base.replace("https://", "http://", 1) if self.base.startswith("https://") \
            else self.base.replace("http://", "https://", 1)
        self.symbol = cfg.get("instrument", symbol.replace("/", "").upper())
        self.scale = float(cfg.get("price_scale", 1000))
        self.workers = max(1, min(12, int(cfg.get("workers", 3))))
        self.timeout = float(cfg.get("timeout_sec", 15))
        self.retries = max(1, int(cfg.get("retries", 4)))
        self.pause = float(cfg.get("pause_sec", 0.0))                 # فاصل صغير بين الطلبات لتخفيف الضغط
        self.max_throttles = int(cfg.get("abort_after_throttles", 8))  # بعد هذا العدد من 429 المتتالية نتوقف بدل إغراق الخادم
        self.cache_dir = cfg.get("cache_dir")                          # None = كاش الذاكرة فقط (الاختبارات)
        self._mem: dict = {}
        self._cooldown_until = 0.0
        self._throttles = 0
        if self.cache_dir:
            os.makedirs(self.cache_dir, exist_ok=True)
            self._prune_cache()
        self.s = session or requests.Session()
        self.s.headers.setdefault("User-Agent", "Mozilla/5.0 goldbot")
        self._lock = threading.Lock()
        self.progress = (0, 0)                    # (hours done, hours total) of the current fetch - shown in /status
        self.stats = {"ok": 0, "missing": 0, "errors": 0}
        self.last_error: str | None = None

    def _count(self, key: str, err: str | None = None):
        with self._lock:
            self.stats[key] += 1
            if err:
                self.last_error = err
            self.progress = (self.progress[0] + 1, self.progress[1])

    def url_for(self, hour: pd.Timestamp, base: str | None = None) -> str:
        # Dukascopy month folders are 00..11 (zero-based) - month-1 is required
        return (f"{base or self.base}/{self.symbol}/{hour.year:04d}/{hour.month - 1:02d}/"
                f"{hour.day:02d}/{hour.hour:02d}h_ticks.bi5")

    @staticmethod
    def decode_ticks(blob: bytes, hour: pd.Timestamp, scale: float = 1000) -> pd.DataFrame:
        raw = lzma.decompress(blob)
        rows = []
        for pos in range(0, len(raw) - 19, 20):
            ms, ask_i, bid_i, ask_vol, bid_vol = struct.unpack(">IIIff", raw[pos:pos + 20])
            bid, ask = bid_i / scale, ask_i / scale
            if bid <= 0 or ask <= 0 or ask < bid:
                continue
            ts = hour + pd.Timedelta(milliseconds=ms)
            rows.append((ts, (bid + ask) / 2.0, float(ask_vol or 0) + float(bid_vol or 0)))
        if not rows:
            return pd.DataFrame(columns=["close", "volume"], index=pd.DatetimeIndex([], tz="UTC"))
        return pd.DataFrame(rows, columns=["time", "close", "volume"]).set_index("time").sort_index()

    @staticmethod
    def _bars(ticks: pd.DataFrame) -> pd.DataFrame:
        """ticks ساعة → شموع 1m (أصغر بكثير في الذاكرة والكاش، والنتيجة النهائية متطابقة لأن الدقيقة لا تعبر الساعة)."""
        if ticks.empty:
            return pd.DataFrame(columns=OHLCV, index=pd.DatetimeIndex([], tz="UTC"))
        bars = ticks["close"].resample("1min").ohlc()
        bars["volume"] = ticks["volume"].resample("1min").sum()
        return bars.dropna(subset=["open", "high", "low", "close"])[OHLCV]

    # ------------------------------------------------------------------ كاش الساعات المكتملة
    @staticmethod
    def _complete(hour: pd.Timestamp) -> bool:
        return hour + pd.Timedelta(hours=1, minutes=10) <= pd.Timestamp.now(tz="UTC")

    def _cache_path(self, hour: pd.Timestamp) -> str:
        return os.path.join(self.cache_dir, f"{self.symbol}_{hour.strftime('%Y%m%d%H')}.pkl")

    def _cache_get(self, hour: pd.Timestamp):
        if hour in self._mem:
            return self._mem[hour]
        if self.cache_dir:
            try:
                df = pd.read_pickle(self._cache_path(hour))
                self._mem[hour] = df
                return df
            except Exception:
                return None
        return None

    def _cache_put(self, hour: pd.Timestamp, bars: pd.DataFrame):
        if not self._complete(hour):
            return                                    # الساعة الحالية ما زالت تتغير: لا تُخزَّن
        self._mem[hour] = bars
        if self.cache_dir:
            try:
                bars.to_pickle(self._cache_path(hour))
            except Exception as e:
                log.warning("تعذّر حفظ كاش الساعة: %s", type(e).__name__)

    def _prune_cache(self, keep_days: int = 21):
        cutoff = time.time() - keep_days * 86400
        try:
            for f in os.listdir(self.cache_dir):
                p = os.path.join(self.cache_dir, f)
                if f.endswith(".pkl") and os.path.getmtime(p) < cutoff:
                    os.remove(p)
        except OSError:
            pass

    # ------------------------------------------------------------------ الطلب مع تراجع ذكي
    def _cooldown(self, seconds: float):
        with self._lock:
            self._cooldown_until = max(self._cooldown_until, time.time() + min(seconds, 60.0))

    def _wait_cooldown(self):
        with self._lock:
            d = self._cooldown_until - time.time()
        if d > 0:
            time.sleep(min(d, 60.0))                  # نوم واحد (لا حلقة انتظار) ليبقى الاختبار والإيقاف سريعين

    def _hour(self, hour: pd.Timestamp):
        empty = pd.DataFrame(columns=OHLCV, index=pd.DatetimeIndex([], tz="UTC"))
        cached = self._cache_get(hour)
        if cached is not None:
            self._count("ok" if not cached.empty else "missing")
            return cached
        if self._throttles >= self.max_throttles:    # الخادم يرفضنا: لا نزيد الضغط، والكاش يحفظ ما نجح للمحاولة القادمة
            self._count("errors", self.last_error or "HTTP 429")
            return empty
        err = None
        for attempt in range(self.retries):
            self._wait_cooldown()
            if self.pause:
                time.sleep(self.pause)
            base = self.alt_base if (err and not err.startswith("HTTP") and attempt % 2 == 1) else self.base   # البديل عند أخطاء الشبكة فقط
            try:
                r = self.s.get(self.url_for(hour, base), timeout=self.timeout)
            except requests.RequestException as e:
                err = f"{type(e).__name__}"
                time.sleep(0.4 * (attempt + 1))
                continue
            if r.status_code == 404:                  # hour not published (weekend / still open / not yet available)
                self._throttles = 0
                self._count("missing")
                if self._complete(hour + pd.Timedelta(hours=2)):
                    self._cache_put(hour, empty)      # ساعات العطلة القديمة لا تُطلب مرة ثانية
                return empty
            if r.status_code == 200:
                self._throttles = 0
                if not r.content:
                    self._count("missing")            # market closed: empty file
                    self._cache_put(hour, empty)
                    return empty
                try:
                    out = self._bars(self.decode_ticks(r.content, hour, self.scale))
                except (lzma.LZMAError, struct.error, ValueError) as e:
                    self._count("errors", f"corrupt file: {type(e).__name__}")
                    return empty
                self._count("ok")
                self._cache_put(hour, out)
                return out
            err = f"HTTP {r.status_code}"             # 403 / 429 / 5xx: blocked or throttled -> back off, retry
            if r.status_code in (429, 503):
                self._throttles += 1
                try:
                    wait = float(r.headers.get("Retry-After", ""))
                except (TypeError, ValueError, AttributeError):
                    wait = 4.0 * (2 ** attempt)       # 4s, 8s, 16s, 32s
                self._cooldown(wait)                  # كل العمال يتوقفون معًا (لا نكرر الطلب من خيوط أخرى)
            else:
                time.sleep(0.8 * (attempt + 1))
        self._count("errors", err)
        return empty

    def fetch(self, interval: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        start = pd.Timestamp(start)
        end = pd.Timestamp(end)
        start = start.tz_localize("UTC") if start.tzinfo is None else start.tz_convert("UTC")
        end = end.tz_localize("UTC") if end.tzinfo is None else end.tz_convert("UTC")
        hours = list(pd.date_range(start.floor("h"), end.ceil("h"), freq="h", inclusive="left", tz="UTC"))
        self.stats = {"ok": 0, "missing": 0, "errors": 0}
        self.progress = (0, len(hours))
        self._throttles = 0
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            parts = list(pool.map(self._hour, hours))
        bars = [p for p in parts if not p.empty]
        e, ok = self.stats["errors"], self.stats["ok"]
        if not bars and e > 0 and e >= self.stats["missing"]:
            # do NOT pretend "no data": the source is unreachable/blocked -> surface the real reason
            raise RuntimeError(f"Dukascopy غير متاح ({e} فشل من {len(hours)} ساعة، آخر خطأ: {self.last_error})")
        if bars and e > 0 and e / max(1, ok + e) > 0.2:
            # بيانات ناقصة بصمت أسوأ من الانتظار: نرفض الآن، وما نجح محفوظ في الكاش فتكتمل المحاولة القادمة
            raise RuntimeError(f"Dukascopy جزئي ({e} فشل مقابل {ok} نجاح؛ المحفوظ يُستكمل في المحاولة التالية، آخر خطأ: {self.last_error})")
        if not bars:
            return pd.DataFrame(columns=OHLCV, index=pd.DatetimeIndex([], tz="UTC"))
        out = pd.concat(bars).sort_index()
        out = out[~out.index.duplicated(keep="last")]
        out = out[(out.index >= start) & (out.index < end)]
        if out.empty:
            return pd.DataFrame(columns=OHLCV, index=pd.DatetimeIndex([], tz="UTC"))
        if interval != "1m":
            tf = {"5m": "5min", "15m": "15min", "30m": "30min", "1h": "1h", "1d": "1D"}.get(interval)
            if tf:
                out = resample_ohlcv(out, tf)[OHLCV]
        return out.astype(float)


class FallbackHistory(HistoryProvider):
    def __init__(self, primary: HistoryProvider, fallback: HistoryProvider):
        self.primary, self.fallback = primary, fallback

    def fetch(self, interval: str, start, end) -> pd.DataFrame:
        try:
            df = self.primary.fetch(interval, start, end)
        except Exception as e:
            log.warning("فشل مزود التاريخ الأساسي (%s) ← استخدام الاحتياطي", e)
            return self.fallback.fetch(interval, start, end)
        minutes = max(1, int((pd.Timestamp(end) - pd.Timestamp(start)).total_seconds() / 60))
        factor = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "1d": 1440}.get(interval, 1)
        expected = max(10, int((minutes / factor) * 0.25))
        if len(df) >= expected:
            return df
        log.warning("Dukascopy returned %d/%d bars; using fallback history provider", len(df), expected)
        return self.fallback.fetch(interval, start, end)

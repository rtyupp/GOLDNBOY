"""Free economic-calendar provider (no API key, no account).

Uses the community ForexFactory weekly JSON (nfs.faireconomy.media), which is public
and keyless. Only HIGH-impact events for a configurable set of currencies are kept.

This is the one deliberate exception to "no news source": the trading decision still
comes from strategies + risk. If the calendar is unreachable the bot does NOT block
(fail-open) and reports the failure in /status and /news.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import List, Optional

import pandas as pd
import requests

log = logging.getLogger("news")

DEFAULT_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
# ForexFactory only publishes the current week (thisweek). The former "nextweek" endpoint
# is a hard 404 now, so the secondary source is empty by default — never point at a dead URL.
DEFAULT_URL_NEXT = None


@dataclass
class NewsEvent:
    ts: pd.Timestamp          # event time in UTC
    currency: str
    impact: str
    title: str

    def label(self) -> str:
        return f"{self.title} ({self.currency})"


def _parse_events(raw, currencies, impacts) -> List[NewsEvent]:
    out: List[NewsEvent] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        try:
            cur = str(item.get("country") or item.get("currency") or "").upper()
            imp = str(item.get("impact") or "").strip().title()
            if cur not in currencies or imp not in impacts:
                continue
            ts = pd.Timestamp(item["date"])
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            out.append(NewsEvent(ts.tz_convert("UTC"), cur, imp, str(item.get("title") or "").strip()))
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda e: e.ts)
    return out


class NewsCalendar:
    def __init__(self, cfg, session: Optional[requests.Session] = None):
        n = cfg.section("news")
        self.enabled = bool(n.get("enabled", True))
        # Only real http(s) URLs are used; empty/"none" values disable a secondary source
        # instead of firing a request at a dead endpoint every refresh.
        self.urls = [u for u in (n.get("url", DEFAULT_URL), n.get("url_next", DEFAULT_URL_NEXT))
                     if u and str(u).strip().lower().startswith(("http://", "https://"))]
        self.currencies = {str(c).upper() for c in n.get("currencies", ["USD", "XAU"])}
        self.impacts = {str(i).strip().title() for i in n.get("impacts", ["High"])}
        self.block_before = float(n.get("block_before_min", 15))
        self.block_after = float(n.get("block_after_min", 15))
        self.refresh_hours = float(n.get("refresh_hours", 6))
        self.timeout = float(n.get("timeout_sec", 15))
        self.cache_path = n.get("cache", "data/cache/news.json")
        self.s = session or requests.Session()
        ua = str(n.get("user_agent", "Mozilla/5.0 (compatible; goldbot/1.0)"))
        try:
            current = self.s.headers.get("User-Agent", "")
            if not current or current.lower().startswith("python-requests"):
                self.s.headers["User-Agent"] = ua
        except (AttributeError, TypeError):
            pass
        self.events: List[NewsEvent] = []
        self.fetched_at = 0.0
        self.last_error: Optional[str] = None
        self._next_try = 0.0            # backoff: don't hammer a 429ing host every refresh
        self._load_cache()

    # ------------------------------------------------------------------ cache
    def _load_cache(self):
        try:
            with open(self.cache_path, encoding="utf-8") as f:
                data = json.load(f)
            self.events = _parse_events(data.get("raw"), self.currencies, self.impacts)
            self.fetched_at = float(data.get("fetched_at", 0))
        except (OSError, ValueError):
            pass

    def _save_cache(self, raw):
        try:
            d = os.path.dirname(self.cache_path)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump({"fetched_at": self.fetched_at, "raw": raw}, f, ensure_ascii=False)
        except OSError:
            pass

    # ------------------------------------------------------------------ fetch
    def due(self, now: Optional[pd.Timestamp] = None) -> bool:
        now = now or pd.Timestamp.now(tz="UTC")
        return (now.timestamp() - self.fetched_at) >= self.refresh_hours * 3600

    def refresh(self, force: bool = False, now: Optional[pd.Timestamp] = None) -> bool:
        if not self.enabled:
            return False
        now = now or pd.Timestamp.now(tz="UTC")
        if time.time() < self._next_try or (not force and not self.due(now)):
            return False
        raw_all, errors, throttled = [], [], False
        for url in self.urls:
            try:
                r = self.s.get(url, timeout=self.timeout)
                if r.status_code == 429:
                    # Rate limited (ForexFactory does this aggressively). Honour Retry-After and back off
                    # instead of logging a fresh error on every refresh.
                    try:
                        wait = float(r.headers.get("Retry-After", "60"))
                    except (TypeError, ValueError):
                        wait = 60.0
                    self._next_try = time.time() + max(60.0, min(wait, 1800.0))
                    throttled = True
                    errors.append("HTTP 429 (rate limited)")
                    continue
                r.raise_for_status()
                data = r.json()
                if isinstance(data, list):
                    raw_all.extend(data)
            except Exception as e:                       # noqa: BLE001 - any network/parse issue => fail-open
                errors.append(f"{type(e).__name__}: {e}")
        if raw_all:
            self.events = _parse_events(raw_all, self.currencies, self.impacts)
            self.fetched_at = now.timestamp()
            self.last_error = None
            self._next_try = 0.0
            self._save_cache(raw_all)
            log.info("تقويم الأخبار: %d حدث مؤثر (حتى %s)", len(self.events),
                     self.events[-1].ts.strftime("%Y-%m-%d") if self.events else "-")
            return True
        # A transient throttle is not a hard failure: keep the last good calendar and stay quiet.
        if throttled and self.events:
            log.info("تقويم الأخبار: المصدر يحجب الطلبات مؤقتًا (429) — نُبقي التقويم السابق ونعيد المحاولة لاحقًا")
            return False
        self.last_error = "؛ ".join(errors) or "لا بيانات"
        log.warning("تعذّر تحديث تقويم الأخبار (%s) — لن يُحجب أي شيء", self.last_error)
        return False

    # ------------------------------------------------------------------ query
    def block_until(self, now: pd.Timestamp) -> Optional[NewsEvent]:
        """يرجع الحدث الذي نحن داخل نافذة حظره الآن، أو None."""
        if not self.enabled or not self.events:
            return None
        for e in self.events:
            start = e.ts - pd.Timedelta(minutes=self.block_before)
            end = e.ts + pd.Timedelta(minutes=self.block_after)
            if start <= now <= end:
                return e
        return None

    def upcoming(self, now: pd.Timestamp, limit: int = 5) -> List[NewsEvent]:
        return [e for e in self.events if e.ts >= now][:limit]

    def status_text(self) -> str:
        if not self.enabled:
            return "معطّل"
        if self.last_error:
            return f"تعذّر التحديث ({self.last_error})"
        if not self.events:
            return "لا أحداث مؤثرة هذا الأسبوع"
        age_h = (pd.Timestamp.now(tz="UTC").timestamp() - self.fetched_at) / 3600 if self.fetched_at else -1
        return f"{len(self.events)} حدث مؤثر (آخر تحديث قبل {age_h:.1f} ساعة)"

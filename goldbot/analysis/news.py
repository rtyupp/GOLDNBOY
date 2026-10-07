"""فلتر الأخبار عالية التأثير.
المصدر الآلي (مجاني، بدون مفتاح): تقويم Forex Factory عبر nfs.faireconomy.media (الأسبوع الحالي + القادم)، يُحدَّث كل ساعة
ويُحفظ كاش محلي. يمكن دمج ملف يدوي data/events.csv (اختياري) أو رابط CSV خاص بك.
قبل الخبر: لا تداول. بعده: انتظار استقرار السوق. إن تعذّر جلب التقويم فترة طويلة: لا تداول (news.require_calendar)."""
from __future__ import annotations
import json, logging, os
from dataclasses import dataclass
from typing import List, Optional
import pandas as pd

log = logging.getLogger("news")
KEYWORDS = ("CPI", "NFP", "NON-FARM", "NONFARM", "FOMC", "PCE", "FED ", "FEDERAL FUNDS", "UNEMPLOYMENT", "GDP",
            "INTEREST RATE", "POWELL", "JOLTS", "ISM ")
FEED_URLS = ["https://nfs.faireconomy.media/ff_calendar_thisweek.json",
             "https://nfs.faireconomy.media/ff_calendar_nextweek.json"]
COLS = ["dt", "name", "impact", "currency", "forecast", "previous"]


@dataclass
class NewsStatus:
    state: str                  # CLEAR | PRE_NEWS | POST_NEWS
    event: Optional[str] = None
    minutes: Optional[float] = None
    calendar_loaded: bool = False
    upcoming: int = 0
    calendar_ok: bool = True    # False = لا توجد بيانات تقويم حديثة (يمنع التداول إن كان مطلوبًا)

    @property
    def blocked(self) -> bool:
        return self.state != "CLEAR"


def _empty() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="object") for c in COLS}).astype({"dt": "datetime64[ns, UTC]"})


def _is_key(name: str, impact: str, currency: str, currencies) -> bool:
    if impact in ("holiday", "non-economic"):
        return False
    if currency not in currencies:
        return False
    return impact == "high" or any(k in (name.upper() + " ") for k in KEYWORDS)


def parse_feed(items: list, currencies=("USD",)) -> pd.DataFrame:
    """يحوّل JSON تقويم Forex Factory (title/country/date/impact/forecast/previous) إلى جدول موحّد."""
    rows = []
    for it in items or []:
        try:
            name = str(it.get("title") or it.get("event") or "").strip()
            cur = str(it.get("country") or it.get("currency") or "").upper().strip()
            impact = str(it.get("impact") or "").lower().strip()
            dt = pd.to_datetime(it.get("date"), utc=True)
            if not name or pd.isna(dt):
                continue
            if _is_key(name, impact, cur, currencies):
                rows.append({"dt": dt, "name": name, "impact": impact, "currency": cur,
                             "forecast": it.get("forecast") or "", "previous": it.get("previous") or ""})
        except Exception:
            continue
    return pd.DataFrame(rows, columns=COLS) if rows else _empty()


def fetch_feed(session, urls=FEED_URLS, currencies=("USD",), timeout: float = 20):
    """يرجع (جدول, أخطاء). يكفي نجاح رابط واحد."""
    frames, errors = [], []
    for u in urls:
        try:
            r = session.get(u, timeout=timeout, headers={"User-Agent": "Mozilla/5.0 goldbot"})
            if r.status_code != 200:
                errors.append(f"{u.rsplit('/', 1)[-1]}: HTTP {r.status_code}")
                continue
            data = r.json()
            if isinstance(data, list):
                frames.append(parse_feed(data, currencies))
        except Exception as e:
            errors.append(f"{u.rsplit('/', 1)[-1]}: {type(e).__name__}")
    if not frames:
        return None, errors
    df = pd.concat(frames, ignore_index=True)
    return df, errors


class NewsFilter:
    def __init__(self, path: str = "data/events.csv", before_min: float = 30, after_min: float = 15,
                 require_calendar: bool = False, max_age_h: float = 36, currencies=("USD",),
                 cache_path: Optional[str] = None):
        self.path, self.before, self.after = path, before_min, after_min
        self.require_calendar, self.max_age_h, self.currencies = require_calendar, max_age_h, tuple(currencies)
        self.cache_path = cache_path
        self.csv_events = self._load_csv()
        self.feed_events = _empty()
        self.last_ok: Optional[pd.Timestamp] = None
        self.last_error: Optional[str] = None
        self.source = "csv"
        self.events = self.csv_events
        self._load_cache()

    # ---------- مصادر
    def _load_csv(self) -> pd.DataFrame:
        if not self.path or not os.path.exists(self.path):
            return _empty()
        try:
            df = pd.read_csv(self.path, comment="#")
        except Exception:
            return _empty()
        if df.empty or "datetime_utc" not in df.columns:
            return _empty()
        df["dt"] = pd.to_datetime(df["datetime_utc"], utc=True)
        df["impact"] = (df["impact"] if "impact" in df.columns else "high")
        df["impact"] = df["impact"].fillna("high").astype(str).str.lower()
        df["name"] = df["name"].astype(str)
        keep = (df["impact"] == "high") | df["name"].str.upper().apply(lambda s: any(k in s + " " for k in KEYWORDS))
        df = df[keep].copy()
        df["currency"], df["forecast"], df["previous"] = "USD", "", ""
        return df[COLS].reset_index(drop=True)

    def _load_cache(self):
        if self.cache_path and os.path.exists(self.cache_path):
            try:
                with open(self.cache_path, encoding="utf-8") as f:
                    j = json.load(f)
                df = pd.DataFrame(j["events"], columns=COLS)
                df["dt"] = pd.to_datetime(df["dt"], utc=True)
                self.feed_events = df
                self.last_ok = pd.Timestamp(j["last_ok"])
                self.source = "forexfactory (كاش)"
                self._merge()
            except Exception as e:
                log.warning("تعذّر قراءة كاش الأخبار: %s", e)

    def apply_feed(self, df: pd.DataFrame, now: Optional[pd.Timestamp] = None):
        now = now or pd.Timestamp.now(tz="UTC")
        self.feed_events, self.last_ok, self.last_error, self.source = df, now, None, "forexfactory"
        self._merge()
        if self.cache_path:
            try:
                os.makedirs(os.path.dirname(self.cache_path) or ".", exist_ok=True)
                out = df.copy()
                out["dt"] = out["dt"].astype(str)
                with open(self.cache_path, "w", encoding="utf-8") as f:
                    json.dump({"last_ok": now.isoformat(), "events": out.values.tolist()}, f, ensure_ascii=False)
            except OSError:
                pass

    def reload(self):
        self.csv_events = self._load_csv()
        self._merge()

    def _merge(self):
        df = pd.concat([self.csv_events, self.feed_events], ignore_index=True)
        if df.empty:
            self.events = _empty()
            return
        df = df.drop_duplicates(subset=["dt", "name"]).sort_values("dt").reset_index(drop=True)
        self.events = df

    # ---------- حالة
    def calendar_ok(self, now: pd.Timestamp) -> bool:
        if not self.require_calendar:
            return True
        if self.last_ok is None:
            return len(self.csv_events) > 0 and bool((self.csv_events["dt"] > now).any())
        return (now - self.last_ok) <= pd.Timedelta(hours=self.max_age_h)

    def status(self, now: pd.Timestamp) -> NewsStatus:
        ev = self.events
        loaded = len(ev) > 0
        upcoming = int((ev["dt"] > now).sum()) if loaded else 0
        ok = self.calendar_ok(now)
        for _, r in ev.iterrows():
            delta = (r["dt"] - now).total_seconds() / 60.0
            if 0 <= delta <= self.before:
                return NewsStatus("PRE_NEWS", r["name"], delta, loaded, upcoming, ok)
            if -self.after <= delta < 0:
                return NewsStatus("POST_NEWS", r["name"], -delta, loaded, upcoming, ok)
        return NewsStatus("CLEAR", None, None, loaded, upcoming, ok)

    def next_events(self, now: pd.Timestamp, k: int = 3) -> List[tuple]:
        ev = self.events[self.events["dt"] > now].head(k)
        return [(r["dt"], r["name"]) for _, r in ev.iterrows()]

    def next_events_full(self, now: pd.Timestamp, k: int = 6) -> List[dict]:
        ev = self.events[self.events["dt"] > now].head(k)
        return [{"time_utc": r["dt"].strftime("%Y-%m-%d %H:%M"), "name": r["name"], "impact": r["impact"],
                 "currency": r["currency"], "forecast": r["forecast"], "previous": r["previous"]} for _, r in ev.iterrows()]

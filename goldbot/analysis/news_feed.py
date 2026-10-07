"""مصادر أخبار حية اختيارية؛ لا تستبدل تقويم الحظر اليدوي."""
from __future__ import annotations

import email.utils
import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable
from urllib.parse import urlparse

import requests

log = logging.getLogger("news_feed")


@dataclass(frozen=True)
class NewsItem:
    title: str
    source: str
    url: str
    published: datetime | None
    summary: str = ""

    @property
    def age_minutes(self) -> float | None:
        if not self.published:
            return None
        return (datetime.now(timezone.utc) - self.published).total_seconds() / 60


class NewsAggregator:
    def __init__(self, sources: Iterable[dict | str] = (), timeout: float = 10, cache_ttl: int = 900,
                 session: requests.Session | None = None):
        self.sources = list(sources or [])
        self.timeout = float(timeout)
        self.cache_ttl = int(cache_ttl)
        self.s = session or requests.Session()
        self.items: list[NewsItem] = []
        self.last_refresh: float | None = None
        self.last_errors: list[str] = []

    def _source(self, raw: dict | str) -> tuple[str, str]:
        if isinstance(raw, str):
            return raw, raw
        return str(raw.get("name") or raw.get("url") or "مصدر"), str(raw.get("url", ""))

    @staticmethod
    def _text(node, names: tuple[str, ...]) -> str:
        for name in names:
            found = node.find(f".//{{*}}{name}")
            if found is None:
                found = node.find(name)
            if found is not None and found.text:
                return " ".join(found.text.split())
        return ""

    @staticmethod
    def _date(value: str) -> datetime | None:
        if not value:
            return None
        try:
            dt = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            try:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    def _parse(self, body: bytes, source: str) -> list[NewsItem]:
        root = ET.fromstring(body)
        rows = list(root.findall(".//item")) + list(root.findall(".//{*}entry"))
        out: list[NewsItem] = []
        for row in rows:
            title = self._text(row, ("title",))
            link = self._text(row, ("link",))
            if not link:
                link_node = row.find(".//{*}link")
                link = (link_node.attrib.get("href", "") if link_node is not None else "")
            if not title or not link or urlparse(link).scheme not in ("http", "https"):
                continue
            out.append(NewsItem(title[:240], source, link, self._date(self._text(row, ("pubDate", "published", "updated"))), self._text(row, ("description", "summary"))[:400]))
        return out

    def refresh(self, force: bool = False) -> list[NewsItem]:
        if not force and self.last_refresh and time.time() - self.last_refresh < self.cache_ttl:
            return self.items
        found: list[NewsItem] = []
        errors: list[str] = []
        for raw in self.sources:
            name, url = self._source(raw)
            if not url:
                continue
            try:
                r = self.s.get(url, timeout=self.timeout, headers={"User-Agent": "GOLDNBOY/1.0 news reader"})
                r.raise_for_status()
                found.extend(self._parse(r.content, name))
            except Exception as exc:
                errors.append(f"{name}: {type(exc).__name__}")
                log.warning("فشل مصدر الأخبار %s: %s", name, exc)
        unique: dict[str, NewsItem] = {}
        for item in sorted(found, key=lambda x: x.published or datetime.min.replace(tzinfo=timezone.utc), reverse=True):
            unique.setdefault(item.url, item)
        self.items, self.last_errors, self.last_refresh = list(unique.values()), errors, time.time()
        return self.items

    def latest(self, limit: int = 8) -> list[NewsItem]:
        return self.items[:limit]

    def format_ar(self, limit: int = 8) -> str:
        if not self.items:
            return "لا توجد أخبار حية متاحة الآن. تحقق من news.sources أو الشبكة."
        lines = ["آخر أخبار السوق (للاطلاع وليست تقويم حظر تداول):"]
        for i, item in enumerate(self.latest(limit), 1):
            when = item.published.strftime("%m-%d %H:%M UTC") if item.published else "وقت غير معروف"
            lines.append(f"{i}. {item.title}\n   {item.source} — {when}\n   {item.url}")
        if self.last_errors:
            lines.append("\nمصادر لم تستجب: " + "، ".join(self.last_errors[:3]))
        return "\n".join(lines)

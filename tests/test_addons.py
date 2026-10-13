"""اختبارات الإضافات: تنبيه «فرصة قاربت»، فلتر الأخبار، وحفظ السجل عبر Gist (كلها بلا شبكة)."""
import base64
import os
import tempfile
import unittest
from unittest import mock

import pandas as pd

from goldbot.providers.news import NewsCalendar, NewsEvent, _parse_events
from goldbot.persist import JournalSync, _safe_copy
from goldbot.tracking.journal import Journal
from tests.helpers import cfg, mkapp, series_from_closes


# ------------------------------------------------------------------ near-miss
class TestNearMiss(unittest.TestCase):
    def _cand(self, score=90):
        from goldbot.engine.pipeline import Candidate
        from goldbot.models import StrategyResult
        r = StrategyResult(name="liquidity_sweep", signal="NONE", score=score, reason="missing: x")
        r.setup_tags["direction"] = "bull"
        c = Candidate(ctx=mock.Mock(), log=mock.Mock())
        c.results = [r]
        return c

    def test_near_miss_sent_once_then_cooldown(self):
        c, app = mkapp(near_miss__score=80, near_miss__cooldown_min=60)
        t0 = pd.Timestamp("2026-10-13 06:00", tz="UTC")
        app._maybe_near_miss(self._cand(90), t0)
        self.assertEqual(app.notify_q.qsize(), 1)
        app._maybe_near_miss(self._cand(90), t0 + pd.Timedelta(minutes=5))     # داخل التهدئة
        self.assertEqual(app.notify_q.qsize(), 1)
        app._maybe_near_miss(self._cand(90), t0 + pd.Timedelta(minutes=61))    # بعد التهدئة
        self.assertEqual(app.notify_q.qsize(), 2)

    def test_below_threshold_is_silent(self):
        c, app = mkapp(near_miss__score=80)
        app._maybe_near_miss(self._cand(70), pd.Timestamp("2026-10-13 06:00", tz="UTC"))
        self.assertEqual(app.notify_q.qsize(), 0)

    def test_paper_mode_is_silent(self):
        c, app = mkapp(near_miss__score=80)
        app.cfg.set("mode", "paper")
        app._maybe_near_miss(self._cand(95), pd.Timestamp("2026-10-13 06:00", tz="UTC"))
        self.assertEqual(app.notify_q.qsize(), 0)


# ------------------------------------------------------------------ news calendar
def _raw(date, cur="USD", imp="High", title="Non-Farm Payrolls"):
    return [{"title": title, "country": cur, "impact": imp, "date": date}]


class TestNewsCalendar(unittest.TestCase):
    def test_parse_filters_currency_and_impact(self):
        raw = _raw("2026-10-13T12:30:00-04:00") + _raw("2026-10-13T12:30:00-04:00", cur="EUR") \
            + _raw("2026-10-13T12:30:00-04:00", imp="Low", title="Minor")
        evs = _parse_events(raw, {"USD", "XAU"}, {"High"})
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0].ts.tz_convert("UTC").hour, 16)   # -04:00 → 16:30 UTC

    def test_block_window(self):
        c = cfg(news__enabled=True, news__cache=os.path.join(tempfile.mkdtemp(), "n.json"))
        nl = NewsCalendar(c, session=mock.Mock())
        nl.events = [NewsEvent(pd.Timestamp("2026-10-13 16:30", tz="UTC"), "USD", "High", "CPI")]
        self.assertIsNotNone(nl.block_until(pd.Timestamp("2026-10-13 16:20", tz="UTC")))   # قبل 10 دقائق (< 15)
        self.assertIsNotNone(nl.block_until(pd.Timestamp("2026-10-13 16:40", tz="UTC")))   # بعد 10 دقائق (< 15)
        self.assertIsNone(nl.block_until(pd.Timestamp("2026-10-13 16:50", tz="UTC")))      # بعد 20 دقيقة
        self.assertIsNone(nl.block_until(pd.Timestamp("2026-10-13 15:00", tz="UTC")))      # قبل ساعة ونصف

    def test_pipeline_gate_blocks_within_window(self):
        from goldbot.engine.pipeline import Pipeline
        c = cfg()
        nl = mock.Mock()
        nl.block_until.return_value = NewsEvent(pd.Timestamp("2026-10-13 16:30", tz="UTC"), "USD", "High", "CPI")
        p = Pipeline(c, journal=None, news=nl)
        closes = [100 + (i % 7) * 0.5 for i in range(400)]
        frames = {tf: series_from_closes(closes, freq=freq)
                  for tf, freq in (("1m", "1min"), ("5m", "5min"), ("15m", "15min"), ("1H", "1h"), ("4H", "4h"))}
        cand = p.prepare(frames, 100.0, 100.2, pd.Timestamp("2026-10-13 16:20", tz="UTC"))
        self.assertFalse(cand.tradable)
        self.assertTrue(any(r.startswith("NEWS_BLACKOUT") for r in cand.reasons))

    def test_provider_fail_open(self):
        c = cfg(news__enabled=True, news__cache=os.path.join(tempfile.mkdtemp(), "n.json"))
        s = mock.Mock()
        s.get.side_effect = RuntimeError("offline")
        nl = NewsCalendar(c, session=s)
        self.assertFalse(nl.refresh(force=True))
        self.assertIsNone(nl.block_until(pd.Timestamp.now(tz="UTC")))   # لا حجب بلا بيانات
        self.assertIn("تعذّر", nl.status_text())

    def test_dead_nextweek_endpoint_is_not_requested(self):
        """Regression: ForexFactory's nextweek endpoint is a permanent 404. It must not be in the
        default URL list (nor an empty config value), so a refresh only hits real endpoints."""
        import goldbot.providers.news as N
        self.assertNotIn("nextweek", (N.DEFAULT_URL_NEXT or ""))
        c = cfg(news__enabled=True, news__cache=os.path.join(tempfile.mkdtemp(), "n.json"),
                news__url_next="")
        nl = NewsCalendar(c, session=mock.Mock())
        self.assertEqual(nl.urls, ["https://nfs.faireconomy.media/ff_calendar_thisweek.json"])
        # and the shipped config.yaml must not point at a dead URL either
        self.assertEqual(len(cfg().section("news").get("url_next") or ""), 0)

    def test_throttled_refresh_keeps_previous_calendar(self):
        """Regression: a 429 must not erase a good calendar nor re-log a hard failure on every
        refresh; it backs off and keeps serving the previous events."""
        c = cfg(news__enabled=True, news__cache=os.path.join(tempfile.mkdtemp(), "n.json"))
        nl = NewsCalendar(c, session=mock.Mock())
        nl.events = [NewsEvent(pd.Timestamp("2026-10-13 16:30", tz="UTC"), "USD", "High", "CPI")]
        r429 = mock.Mock(status_code=429, headers={"Retry-After": "120"})
        s = mock.Mock(); s.get.return_value = r429
        nl.s = s
        self.assertFalse(nl.refresh(force=True))              # لم يُحدَّث
        self.assertIsNone(nl.last_error)                       # لكنه ليس خطأً صريحًا
        self.assertEqual(len(nl.events), 1)                    # التقويم السابق محفوظ
        self.assertGreater(nl._next_try, 0)                    # مع تأجيل إعادة المحاولة
        # within the backoff window a forced refresh is skipped (no request fired)
        s.get.reset_mock()
        self.assertFalse(nl.refresh(force=True))
        self.assertEqual(s.get.call_count, 0)


# ------------------------------------------------------------------ persistence (Gist)
class _FakeGitHub:
    def __init__(self):
        self.gists = {}
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(("GET", url))
        m = mock.Mock()
        if url.endswith("/gists"):
            m.json.return_value = [{"id": gid, "files": {g["name"]: g}} for gid, g in self.gists.items()]
            m.raise_for_status.return_value = None
            return m
        gid = url.rsplit("/", 1)[-1]
        g = self.gists[gid]
        m.json.return_value = {"files": {g["name"]: {"content": g["content"]}}}
        m.raise_for_status.return_value = None
        return m

    def post(self, url, **kw):
        self.calls.append(("POST", url))
        body = kw["json"]
        self.gists["g1"] = {"name": next(iter(body["files"])), "content": body["files"][next(iter(body["files"]))]["content"],
                            "public": body.get("public")}
        m = mock.Mock(); m.json.return_value = {"id": "g1"}; m.raise_for_status.return_value = None
        return m

    def patch(self, url, **kw):
        self.calls.append(("PATCH", url))
        gid = url.rsplit("/", 1)[-1]
        body = kw["json"]
        self.gists[gid] = {"name": next(iter(body["files"])), "content": body["files"][next(iter(body["files"]))]["content"]}
        m = mock.Mock(); m.json.return_value = {"id": gid}; m.raise_for_status.return_value = None
        return m


class TestPersist(unittest.TestCase):
    def test_push_creates_private_gist_then_updates(self):
        tmp = tempfile.mkdtemp()
        jp = os.path.join(tmp, "j.sqlite")
        Journal(jp)                       # ينشئ قاعدة بيانات سليمة
        c = cfg(paths__journal=jp, persistence__enabled=True)
        gh = _FakeGitHub()
        ps = JournalSync(c, token="tok", session=gh)
        self.assertTrue(ps.push(force=True))
        self.assertEqual(gh.calls[0][0], "GET")              # يبحث عن نسخة قائمة أولًا
        self.assertIn(("POST", "https://api.github.com/gists"), gh.calls)
        self.assertFalse(gh.gists["g1"]["public"])           # خاص، ليس عامًا
        b64 = gh.gists["g1"]["content"]
        self.assertTrue(base64.b64decode(b64).startswith(b"SQLite format 3"))
        self.assertTrue(ps.push(force=True))                 # المرة الثانية تحديث (PATCH)
        self.assertEqual(gh.calls[-1][0], "PATCH")

    def test_restore_only_when_local_missing(self):
        tmp = tempfile.mkdtemp()
        jp = os.path.join(tmp, "j.sqlite")
        src = os.path.join(tmp, "src.sqlite")
        sj = Journal(src)
        sj.add_signal(timestamp="2026-10-13T00:00:00+00:00", symbol="XAUUSD", direction="BUY", entry=1.0, sl=0.9,
                      tp1=1.1, tp2=1.2, strategy="x", session="Asian", score=90, rr1=1.2, rr2=2.4)
        sj.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")     # ادمج WAL في الملف الرئيسي
        with open(src, "rb") as fh:
            data = fh.read()
        c = cfg(paths__journal=jp, persistence__enabled=True)
        gh = _FakeGitHub()
        gh.gists["g1"] = {"name": c.get("persistence.filename"), "content": base64.b64encode(data).decode()}
        with mock.patch("goldbot.persist.time.time", return_value=1000.0):
            ps = JournalSync(c, token="tok", session=gh)
            self.assertTrue(ps.restore())
            self.assertTrue(os.path.exists(jp))
            # محلي ممتلئ ⇒ لا استعادة (نحمي بيانات أحدث) — حتى لو مر وقت طويل
            with mock.patch("goldbot.persist.time.time", return_value=99999.0):
                self.assertFalse(ps.restore())

    def test_restore_replaces_file_under_a_live_wal_connection(self):
        """Regression: the app opens the Journal BEFORE restore(). Overwriting journal.sqlite in
        place while that WAL connection is open leaves stale -wal/-shm sidecars that SQLite replays
        over the restored file, wiping the restored rows. restore() must swap the file safely and
        the SAME live handle must then see the restored data."""
        tmp = tempfile.mkdtemp()
        jp = os.path.join(tmp, "j.sqlite")
        # A backup with real rows, copied exactly like push() does (checkpoint + backup)
        src = os.path.join(tmp, "src.sqlite")
        sj = Journal(src)
        sj.add_signal(timestamp="2026-10-13T00:00:00+00:00", symbol="XAUUSD", direction="BUY", entry=1.0, sl=0.9,
                      tp1=1.1, tp2=1.2, strategy="x", session="Asian", score=90, rr1=1.2, rr2=2.4)
        tmpcopy = os.path.join(tempfile.mkdtemp(), "goldbot-journal.sqlite")
        self.assertTrue(_safe_copy(src, tmpcopy))
        with open(tmpcopy, "rb") as fh:
            data = fh.read()

        # Production ordering: live Journal on the target first (opens the empty file), then restore
        live = Journal(jp)
        self.assertEqual(len(live.last_signals(10)), 0)
        c = cfg(paths__journal=jp, persistence__enabled=True)
        gh = _FakeGitHub()
        gh.gists["g1"] = {"name": c.get("persistence.filename"), "content": base64.b64encode(data).decode()}
        ps = JournalSync(c, token="tok", session=gh, journal=live)
        self.assertTrue(ps.restore())
        # Same handle must now expose the restored row (and remain usable)
        self.assertEqual(len(live.last_signals(10)), 1)
        live.add_signal(timestamp="2026-10-14T00:00:00+00:00", symbol="XAUUSD", direction="SELL", entry=1.0, sl=1.1,
                        tp1=0.9, tp2=0.8, strategy="y", session="Asian", score=90, rr1=1.2, rr2=2.4)
        self.assertEqual(len(live.last_signals(10)), 2)

    def test_disabled_without_token(self):
        c = cfg(persistence__enabled=True)
        ps = JournalSync(c, token="", session=mock.Mock())
        self.assertFalse(ps.ready)
        self.assertFalse(ps.push(force=True))


if __name__ == "__main__":
    unittest.main()

"""Guards for the all-free data stack and the Render/Telegram fixes."""
import asyncio, lzma, struct, unittest
import pandas as pd
from goldbot.config import load_config
from goldbot.providers.dukascopy import DukascopyHistory
from goldbot.providers.factory import make_history_provider, make_live_provider
from goldbot.providers.swissquote import SwissquotePublic
from goldbot.notify.telegram import Telegram, CommandBot


class Resp:
    def __init__(self, code=200, content=b"", payload=None):
        self.status_code, self.content, self._p, self.text = code, content, payload, ""

    def json(self):
        return self._p


class FakeSession:
    def __init__(self, resp_for):
        self.resp_for, self.urls, self.headers, self.posts = resp_for, [], {}, []

    def get(self, url, **kw):
        self.urls.append(url)
        return self.resp_for(url)

    def post(self, url, data=None, files=None, timeout=None):
        self.posts.append((url, dict(data or {}), timeout))
        return Resp(200, payload={"ok": True, "result": []})


class TestDukascopyUrl(unittest.TestCase):
    def test_month_is_zero_based(self):
        d = DukascopyHistory({})
        self.assertIn("/2026/09/09/07h_ticks.bi5", d.url_for(pd.Timestamp("2026-10-09 07:00", tz="UTC")))   # October -> 09
        self.assertIn("/2026/00/02/00h_ticks.bi5", d.url_for(pd.Timestamp("2026-01-02 00:00", tz="UTC")))   # January -> 00
        self.assertIn("/2026/11/31/23h_ticks.bi5", d.url_for(pd.Timestamp("2026-12-31 23:00", tz="UTC")))

    def test_fetch_builds_bars_from_a_published_hour(self):
        hour = pd.Timestamp("2026-10-07 10:00", tz="UTC")
        raw = b"".join(struct.pack(">IIIff", ms, 2650500 + k, 2650000 + k, 1.0, 1.0) for k, ms in enumerate((1000, 30000, 61000)))
        blob = lzma.compress(raw)
        sess = FakeSession(lambda u: Resp(200, blob) if "/2026/09/07/10h_" in u else Resp(404))
        d = DukascopyHistory({"workers": 1}, session=sess)
        df = d.fetch("1m", hour, hour + pd.Timedelta(hours=2))
        self.assertEqual(len(df), 2)                                  # minute 0 (2 ticks) + minute 1
        self.assertAlmostEqual(float(df.iloc[0].open), 2650.25, places=2)

    def test_404_is_not_an_error(self):
        d = DukascopyHistory({"workers": 1}, session=FakeSession(lambda u: Resp(404)))
        self.assertTrue(d.fetch("1m", pd.Timestamp("2026-10-07 10:00", tz="UTC"), pd.Timestamp("2026-10-07 12:00", tz="UTC")).empty)


class TestDukascopyBlocked(unittest.TestCase):
    def test_blocked_source_raises_a_clear_error_instead_of_silent_empty(self):
        d = DukascopyHistory({"workers": 1, "retries": 1}, session=FakeSession(lambda u: Resp(429)))
        import goldbot.providers.dukascopy as m
        real, m.time.sleep = m.time.sleep, lambda x: None
        try:
            with self.assertRaises(RuntimeError) as cm:
                d.fetch("1m", pd.Timestamp("2026-10-07 10:00", tz="UTC"), pd.Timestamp("2026-10-07 12:00", tz="UTC"))
        finally:
            m.time.sleep = real
        self.assertIn("429", str(cm.exception))
        self.assertEqual(d.progress[0], d.progress[1])


class TestBuiltinNews(unittest.TestCase):
    def test_nfp_and_fomc_with_dst(self):
        from goldbot.analysis.news import builtin_events
        df = builtin_events(pd.Timestamp("2026-10-09 10:40", tz="UTC"))
        got = {r["name"].split()[0]: r["dt"] for _, r in df.iterrows()}
        self.assertEqual(got["Non-Farm"], pd.Timestamp("2026-11-06 13:30", tz="UTC"))   # first Friday, EST (after DST ends)
        self.assertEqual(got["FOMC"], pd.Timestamp("2026-10-28 18:00", tz="UTC"))       # 2pm EDT


class TestFactoryIsFree(unittest.TestCase):
    def test_default_config_needs_no_keys(self):
        import os
        for k in ("SIFTING_API_KEY", "OANDA_API_TOKEN", "OANDA_ACCOUNT_ID"):
            os.environ.pop(k, None)
        cfg = load_config("config/config.yaml")
        self.assertIsInstance(make_history_provider(cfg), DukascopyHistory)
        self.assertIsInstance(make_live_provider(cfg, lambda t: None, lambda s: None), SwissquotePublic)

    def test_oanda_without_credentials_falls_back_to_free(self):
        import os
        os.environ.pop("OANDA_API_TOKEN", None)
        cfg = load_config("config/config.yaml")
        cfg.set("providers.live", "oanda")
        cfg.set("providers.history", "oanda")
        self.assertIsInstance(make_history_provider(cfg), DukascopyHistory)
        self.assertIsInstance(make_live_provider(cfg, lambda t: None, lambda s: None), SwissquotePublic)

    def test_no_paid_provider_left_in_config(self):
        txt = open("config/config.yaml", encoding="utf-8").read().lower()
        self.assertNotIn("siftingio:", txt)
        self.assertNotIn("sifting_api_key", txt)


class TestSwissquoteDedupe(unittest.TestCase):
    def test_identical_snapshot_is_not_a_new_tick(self):
        payload = [{"ts": 1_700_000_000_000, "spreadProfilePrices": [{"bid": 2650.0, "ask": 2650.3}]}]
        got = []
        sess = FakeSession(lambda u: Resp(200, payload=payload))
        p = SwissquotePublic({}, "XAUUSD", got.append, session=sess, clock=lambda: 1_700_000_001.0)
        p._once(); p._once(); p._once()
        self.assertEqual(len(got), 1)
        self.assertEqual(p.skipped_duplicates, 2)


class TestTelegramLongPoll(unittest.TestCase):
    def test_get_updates_is_real_long_polling(self):
        s = FakeSession(lambda u: Resp())
        tg = Telegram("T", None, [1], session=s)
        tg.get_updates(None, 25)
        url, data, http_timeout = s.posts[-1]
        self.assertTrue(url.endswith("/getUpdates"))
        self.assertEqual(data["timeout"], 25)                          # sent to Telegram (previously dropped!)
        self.assertGreater(http_timeout, 25)                           # HTTP timeout longer than the long poll
        self.assertNotIn("offset", data)
        tg.get_updates(7, 25)
        self.assertEqual(s.posts[-1][1]["offset"], 7)

    def test_conflict_is_retried_not_fatal(self):
        class TG:
            admin_ids = [1]
            last_error = None
            n = 0
            def delete_webhook(self): return True
            def get_updates(self, off, t):
                self.n += 1
                if self.n == 1:
                    self.last_error = "getUpdates: Conflict: terminated by other getUpdates request"
                    return []
                bot._stop = True
                return []
        tg = TG()
        bot = CommandBot(tg, {})

        async def go():
            real = asyncio.sleep
            async def fast(x, *a, **k): await real(0)
            asyncio.sleep = fast
            try:
                await bot.run()
            finally:
                asyncio.sleep = real
        asyncio.run(go())
        self.assertGreaterEqual(tg.n, 2)                               # kept polling after the 409


if __name__ == "__main__":
    unittest.main()

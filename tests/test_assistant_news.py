import asyncio, json, os, tempfile, unittest
import pandas as pd
from tests.helpers import cfg, FakeTG
from tests.test_e2e import find_tradable
from tests.test_arabic_private import Resp, FakeSession
from goldbot.analysis.news import parse_feed, fetch_feed, fetch_ff, NewsFilter, NewsStatus, FEED_HOSTS
from goldbot.analysis.news_ai import parse_ai_events, fetch_via_gemini
from goldbot.ai.assistant import Assistant, parse_chart_request
from goldbot.ai.gemini_layer import GeminiLayer
from goldbot.app import BotApp
from goldbot.engine import no_trade
from goldbot.notify.telegram import CommandBot
from goldbot.providers.synthetic import SyntheticHistory

SAMPLE = [
    {"title": "CPI m/m", "country": "USD", "date": "2026-10-14T08:30:00-04:00", "impact": "High", "forecast": "0.3%", "previous": "0.4%"},
    {"title": "Non-Farm Employment Change", "country": "USD", "date": "2026-11-06T08:30:00-05:00", "impact": "High", "forecast": "150K", "previous": "140K"},
    {"title": "Fed Chair Powell Speaks", "country": "USD", "date": "2026-10-15T14:00:00-04:00", "impact": "Medium", "forecast": "", "previous": ""},
    {"title": "German Ifo", "country": "EUR", "date": "2026-10-14T04:00:00-04:00", "impact": "High", "forecast": "", "previous": ""},
    {"title": "Retail Sales", "country": "USD", "date": "2026-10-16T08:30:00-04:00", "impact": "Low", "forecast": "", "previous": ""},
    {"title": "Columbus Day", "country": "USD", "date": "2026-10-12T00:00:00-04:00", "impact": "Holiday", "forecast": "", "previous": ""},
    {"title": "broken", "country": "USD", "date": "not-a-date", "impact": "High"},
]


class TestNewsFeed(unittest.TestCase):
    def test_parse_filters_and_converts_to_utc(self):
        df = parse_feed(SAMPLE)
        self.assertEqual(sorted(df["name"]), ["CPI m/m", "Fed Chair Powell Speaks", "Non-Farm Employment Change"])
        cpi = df[df["name"] == "CPI m/m"].iloc[0]
        self.assertEqual(cpi["dt"], pd.Timestamp("2026-10-14 12:30", tz="UTC"))
        self.assertEqual(cpi["forecast"], "0.3%")

    def test_fetch_tolerates_one_failed_url(self):
        class S:
            def get(self, url, **k):
                return Resp(200, SAMPLE) if "thisweek" in url else Resp(404)
        df, errs = fetch_feed(S(), ["x/ff_calendar_thisweek.json", "x/ff_calendar_nextweek.json"])
        self.assertEqual(len(df), 3)
        self.assertEqual(len(errs), 1)

    def test_fetch_all_fail_returns_none(self):
        class S:
            def get(self, url, **k):
                raise ConnectionError("down")
        df, errs = fetch_feed(S(), ["a", "b"])
        self.assertIsNone(df)
        self.assertEqual(len(errs), 2)

    def test_filter_windows_cache_and_staleness(self):
        d = tempfile.mkdtemp()
        cache = os.path.join(d, "n.json")
        nf = NewsFilter(os.path.join(d, "none.csv"), 30, 15, require_calendar=True, max_age_h=36, cache_path=cache)
        now = pd.Timestamp("2026-10-14 12:10", tz="UTC")
        self.assertFalse(nf.calendar_ok(now))                       # لا بيانات أبدًا
        nf.apply_feed(parse_feed(SAMPLE), now)
        self.assertTrue(nf.calendar_ok(now))
        self.assertEqual(nf.status(now).state, "PRE_NEWS")          # CPI 12:30
        self.assertEqual(nf.status(pd.Timestamp("2026-10-14 12:40", tz="UTC")).state, "POST_NEWS")
        self.assertEqual(nf.status(pd.Timestamp("2026-10-14 13:00", tz="UTC")).state, "CLEAR")
        nf2 = NewsFilter(os.path.join(d, "none.csv"), 30, 15, require_calendar=True, cache_path=cache)   # إعادة تشغيل: من الكاش
        self.assertEqual(len(nf2.events), 3)
        self.assertFalse(nf2.calendar_ok(now + pd.Timedelta(hours=40)))
        full = nf2.next_events_full(pd.Timestamp("2026-10-01", tz="UTC"), 2)
        self.assertEqual(full[0]["name"], "CPI m/m")

    def test_gate_blocks_when_calendar_unavailable(self):
        df, cand = find_tradable()
        cand.ctx.news = NewsStatus("CLEAR", calendar_ok=False)
        r = no_trade.global_gates(cfg(), cand.ctx, True, "OK")
        self.assertIn("NEWS_CALENDAR_UNAVAILABLE", r)



class H:
    def __init__(self, code, js=None, headers=None):
        self.status_code, self._js, self.headers = code, js, headers or {}

    def json(self):
        return self._js


class TestFF(unittest.TestCase):
    def test_429_then_second_host_ok_and_nextweek_404_is_benign(self):
        class S:
            def __init__(self):
                self.urls = []

            def get(self, url, **k):
                self.urls.append(url)
                if "https://nfs." in url:
                    return H(429, None, {"Retry-After": "120"})
                return H(200, SAMPLE) if "thisweek" in url else H(404)
        s = S()
        r = fetch_ff(s)
        self.assertIsNotNone(r.df)
        self.assertEqual(len(r.df), 3)
        self.assertTrue(any("429" in e for e in r.errors))
        self.assertFalse(any("nextweek" in e for e in r.errors))      # 404 للأسبوع القادم ليس خطأ
        self.assertTrue(s.urls[0].startswith("https://nfs."))
        self.assertTrue(any(u.startswith("https://cdn-nfs.") for u in s.urls))

    def test_all_hosts_rate_limited_returns_retry_after(self):
        class S:
            def get(self, url, **k):
                return H(429, None, {"Retry-After": "300"})
        r = fetch_ff(S())
        self.assertIsNone(r.df)
        self.assertTrue(r.rate_limited)
        self.assertEqual(r.retry_after, 300.0)

    def test_network_errors_do_not_raise(self):
        class S:
            def get(self, url, **k):
                raise ConnectionError("x")
        r = fetch_ff(S())
        self.assertIsNone(r.df)
        self.assertEqual(len(r.errors), len(FEED_HOSTS))


class TestAINews(unittest.TestCase):
    def test_parse_ai_events_lenient(self):
        txt = '```json\n[{"date_utc":"2026-10-14 12:30","name":"CPI m/m","impact":"high"},{"date_utc":"bad","name":"X"},{"date_utc":"2026-10-15 18:00","name":"FOMC Statement","impact":"high"}]\n```'
        df = parse_ai_events(txt)
        self.assertEqual(list(df["name"]), ["CPI m/m", "FOMC Statement"])
        self.assertEqual(len(parse_ai_events("no json here")), 0)

    def test_fetch_via_gemini_uses_search_tool_and_drops_past(self):
        os.environ["GEMINI_API_KEY"] = "k"
        body = {"candidates": [{"content": {"parts": [{"text": '[{"date_utc":"2026-10-14 12:30","name":"CPI m/m"},{"date_utc":"2026-10-01 12:30","name":"NFP"}]'}]}}]}
        s = FakeSession(lambda *a: Resp(200, body))
        g = GeminiLayer(cfg(), session=s)
        df = fetch_via_gemini(g, pd.Timestamp("2026-10-10", tz="UTC"))
        self.assertEqual(list(df["name"]), ["CPI m/m"])
        self.assertIn({"google_search": {}}, s.calls[0]["json"]["tools"])

    def test_manual_event_blocks_trading_window(self):
        d = tempfile.mkdtemp()
        nf = NewsFilter(os.path.join(d, "e.csv"), 30, 15)
        nf.add_manual(pd.Timestamp("2026-10-14 12:30", tz="UTC"), "CPI, m/m")
        self.assertEqual(nf.status(pd.Timestamp("2026-10-14 12:10", tz="UTC")).state, "PRE_NEWS")
        nf2 = NewsFilter(os.path.join(d, "e.csv"), 30, 15)                    # يبقى بعد إعادة التشغيل
        self.assertEqual(len(nf2.events), 1)


def mkapp(**over):
    c = cfg(paths__journal=os.path.join(tempfile.mkdtemp(), "j.sqlite"), paths__log_dir=tempfile.mkdtemp(),
            news__cache_path=os.path.join(tempfile.mkdtemp(), "n.json"), bootstrap__marker=os.path.join(tempfile.mkdtemp(), "b.json"),
            ml__model_path=os.path.join(tempfile.mkdtemp(), "m.pkl"), **over)
    return c, BotApp(c, tg=FakeTG(), history_provider=SyntheticHistory())


def text_resp(t):
    return Resp(200, {"candidates": [{"content": {"role": "model", "parts": [{"text": t}]}}]})


class TestChartIntent(unittest.TestCase):
    def test_parse(self):
        cases = {"ابي صورة الشارت": "5m", "شارت 15 دقيقة": "15m", "ارسل الشارت ساعة": "1H", "شارت ٤ ساعات": "4H",
                 "chart 4h": "4H", "صورة شارت الدقيقة": "1m", "شارت خمس دقائق": "5m", "كم السعر؟": None, "وش الأخبار": None}
        for t, want in cases.items():
            self.assertEqual(parse_chart_request(t), want, t)


class TestAssistant(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        os.environ["GEMINI_API_KEY"] = "k"
        os.environ.pop("GEMINI_MODEL", None)

    async def ready(self, responder, **over):
        df, cand = find_tradable()
        c, app = mkapp(**over)
        app.last_cand = cand
        s = FakeSession(responder)
        app.gemini = GeminiLayer(c, session=s)
        return c, app, s

    async def test_single_fast_call_with_live_context_and_thinking_low(self):
        c, app, s = await self.ready(lambda *a: text_resp("**السعر** كذا\n# عنوان"))
        reply, photos = await Assistant(app).answer(1, "كم السعر وش الأخبار؟")
        self.assertEqual(len(s.calls), 1)                              # استدعاء واحد فقط
        body = s.calls[0]["json"]
        self.assertNotIn("tools", body)
        text = body["contents"][-1]["parts"][0]["text"]
        for w in ("بيانات البوت الحية", "== السعر ==", "== التحليل ==", "== الأخبار ==", "كم السعر"):
            self.assertIn(w, text)
        self.assertEqual(body["generationConfig"]["thinkingConfig"], {"thinkingLevel": "low"})
        self.assertNotIn("**", reply)
        self.assertNotIn("#", reply)

    async def test_chart_sent_immediately_and_seen_by_model(self):
        c, app, s = await self.ready(lambda *a: text_resp("الشارت يظهر اتجاهًا صاعدًا"))
        sent = []

        async def send_photo(png):
            sent.append(png)
        reply, photos = await Assistant(app).answer(1, "ابي صورة شارت 15 دقيقة", send_photo=send_photo)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][:4], b"\x89PNG")
        self.assertEqual(photos, [])
        self.assertTrue(any("inlineData" in p for p in s.calls[0]["json"]["contents"][-1]["parts"]))
        self.assertEqual(reply, "الشارت يظهر اتجاهًا صاعدًا")

    async def test_chart_still_delivered_when_ai_fails(self):
        c, app, s = await self.ready(lambda *a: Resp(500))
        sent = []

        async def send_photo(png):
            sent.append(png)
        reply, _ = await Assistant(app).answer(1, "الشارت", send_photo=send_photo)
        self.assertEqual(len(sent), 1)
        self.assertIn("الشارت أُرسل", reply)

    async def test_ai_failure_returns_local_summary_never_silence(self):
        c, app, s = await self.ready(lambda *a: Resp(500))
        reply, _ = await Assistant(app).answer(1, "وش وضع الذهب؟")
        self.assertIn("ملخص من بيانات البوت", reply)
        self.assertIn("الاتجاه", reply)

    async def test_429_on_primary_switches_to_fallback_model(self):
        body = text_resp("رد من البديل")
        c, app, s = await self.ready(lambda url, d, j: Resp(429) if "gemini-3.8-flash:" in url else body)
        reply, _ = await Assistant(app).answer(1, "مرحبا")
        self.assertEqual(reply, "رد من البديل")
        self.assertIn("gemini-3.7-flash", s.calls[-1]["url"])

    async def test_unsupported_thinking_param_is_dropped_and_remembered(self):
        def responder(url, d, j):
            if "thinkingConfig" in j.get("generationConfig", {}):
                return Resp(400, None) if False else _R400
            return text_resp("تمام")
        class R400(Resp):
            text = "Unknown field thinkingLevel: thinking config not supported"
        _R400 = R400(400)
        c, app, s = await self.ready(responder)
        r1, _ = await Assistant(app).answer(1, "اختبار")
        self.assertEqual(r1, "تمام")
        self.assertTrue(app.gemini._no_thinking)
        n = len(s.calls)
        await Assistant(app).answer(2, "اختبار ثاني")
        self.assertEqual(len(s.calls), n + 1)                           # لا إعادة محاولة بعد التذكّر

    async def test_user_photo_retry_without_images_on_400(self):
        def responder(url, d, j):
            return Resp(400) if "inlineData" in json.dumps(j) else text_resp("رد بدون صورة")
        c, app, s = await self.ready(responder)
        reply, _ = await Assistant(app).answer(1, "حلل", image=b"\xff\xd8jpeg")
        self.assertEqual(reply, "رد بدون صورة")
        self.assertIn("inlineData", json.dumps(s.calls[0]["json"]))
        self.assertNotIn("inlineData", json.dumps(s.calls[-1]["json"]))

    async def test_memory_between_turns(self):
        c, app, s = await self.ready(lambda *a: text_resp("رد"))
        a = Assistant(app)
        await a.answer(7, "السؤال الأول")
        await a.answer(7, "السؤال الثاني")
        self.assertIn("السؤال الأول", json.dumps(s.calls[1]["json"]["contents"], ensure_ascii=False))

    async def test_adapter_mode(self):
        c, app, s = await self.ready(lambda *a: text_resp("x"))
        got = []
        app.gemini.adapter = lambda p: got.append(p) or "جواب من دالتي"
        reply, _ = await Assistant(app).answer(1, "كم السعر؟")
        self.assertEqual(reply, "جواب من دالتي")
        self.assertIn("بيانات البوت الحية", got[0])

    async def test_no_analysis_yet_for_chart(self):
        c, app = mkapp()
        reply, _ = await Assistant(app).answer(1, "شارت")
        self.assertIn("لا يوجد تحليل جاهز", reply)

    async def test_signal_review_sends_chart_image_and_medium_thinking(self):
        c, app, s = await self.ready(lambda *a: text_resp('{"decision":"BUY","reason":"الشارت يؤكد"}'))
        d = await app.gemini.decide("card", "BUY", image=b"\x89PNGfake")
        self.assertEqual(d.decision, "BUY")
        parts = s.calls[0]["json"]["contents"][0]["parts"]
        self.assertTrue(any("inlineData" in p for p in parts))
        self.assertEqual(s.calls[0]["json"]["generationConfig"]["thinkingConfig"], {"thinkingLevel": "medium"})


class TgAI(FakeTG):
    def __init__(self):
        super().__init__()
        self.photos_sent, self.actions, self.dl = [], [], 0

    def send_chat_action(self, chat, action="typing"):
        self.actions.append(chat)

    def download_photo(self, m):
        self.dl += 1
        return b"IMG"

    def send_photo(self, chat, png, caption=""):
        self.photos_sent.append((chat, len(png)))


class TestRouting(unittest.IsolatedAsyncioTestCase):
    U = staticmethod(lambda uid, **m: {"update_id": 1, "message": {"from": {"id": uid}, "chat": {"id": uid}, **m}})

    async def test_admin_text_and_photo_go_to_ai_strangers_ignored(self):
        tg = TgAI()
        bot = CommandBot(tg, {"/price": lambda a: "PRICE"})
        calls = []

        async def ai(chat, text, img, send_photo=None):
            calls.append((chat, text, img))
            if send_photo:
                await send_photo(b"CHART")
            return "رد الذكاء", [b"PNGDATA"]
        bot.ai = ai
        await bot.handle_update(self.U(1, text="كم سعر الذهب؟"))
        await bot.handle_update(self.U(1, caption="حلل", photo=[{"file_id": "a", "file_size": 1}]))
        await bot.handle_update(self.U(999, text="مرحبا"))
        await bot.handle_update(self.U(1, text="/price"))
        self.assertEqual([c[1] for c in calls], ["كم سعر الذهب؟", "حلل"])
        self.assertEqual(calls[1][2], b"IMG")
        self.assertIn((1, "رد الذكاء"), tg.private)
        self.assertIn((1, "PRICE"), tg.private)
        self.assertEqual(len(tg.photos_sent), 4)                       # شارت فوري + صورة متبقية × رسالتين
        self.assertNotIn(999, [c[0] for c in calls])

    async def test_slow_ai_does_not_block_commands_and_sends_ack(self):
        tg = TgAI()
        bot = CommandBot(tg, {"/price": lambda a: "PRICE"})
        bot.typing_interval, bot.ack_after = 0.05, 0.1

        async def slow_ai(chat, text, img, send_photo=None):
            await asyncio.sleep(0.5)
            return "رد متأخر", []
        bot.ai = slow_ai
        t1 = asyncio.create_task(bot._safe(self.U(1, text="سؤال طويل")))
        await asyncio.sleep(0.05)
        await bot._safe(self.U(1, text="/price"))                      # الأمر يُجاب فورًا رغم أن الذكاء الاصطناعي مشغول
        self.assertIn((1, "PRICE"), tg.private)
        self.assertNotIn((1, "رد متأخر"), tg.private)
        await t1
        self.assertIn((1, "رد متأخر"), tg.private)
        self.assertTrue(any("جارٍ التحليل" in m for _, m in tg.private))

    async def test_ai_exception_still_replies(self):
        tg = TgAI()
        bot = CommandBot(tg, {})

        async def boom(chat, text, img, send_photo=None):
            raise RuntimeError("x")
        bot.ai = boom
        await bot._safe(self.U(1, text="مرحبا"))
        self.assertTrue(any("حدث خطأ غير متوقع" in m for _, m in tg.private))


class TestSupervisorAndNewsRefresh(unittest.IsolatedAsyncioTestCase):
    async def test_crashing_task_is_restarted(self):
        c, app = mkapp()
        n = {"i": 0}

        async def flaky():
            n["i"] += 1
            if n["i"] < 3:
                raise RuntimeError("crash")
        import goldbot.app as A
        orig = asyncio.sleep
        async def fast(d, *a, **k):
            await orig(0)
        A.asyncio.sleep = fast
        try:
            await app._supervise("اختبار", flaky)
        finally:
            A.asyncio.sleep = orig
        self.assertEqual(n["i"], 3)

    def test_refresh_backoff_and_gemini_fallback(self):
        import goldbot.app as A
        from goldbot.analysis.news import FetchResult, parse_feed as pf
        os.environ["GEMINI_API_KEY"] = "k"
        c, app = mkapp()
        limited = FetchResult(None, ["nfs: HTTP 429"], 0.0, True)
        A.fetch_ff = lambda *a, **k: limited
        A.fetch_via_gemini = lambda g, now, cur: pf(SAMPLE)
        d1 = app._refresh_events()
        self.assertEqual(app.news.source.split()[0], "gemini-search")      # احتياطي لأن لا يوجد تقويم حديث
        self.assertGreaterEqual(app.news.events.shape[0], 3)
        self.assertEqual(d1, 300.0)                                         # أول فشل: 5 دقائق
        d2 = app._refresh_events()
        self.assertEqual(d2, 600.0)                                         # backoff متصاعد
        A.fetch_via_gemini = lambda *a: (_ for _ in ()).throw(AssertionError("لا يجب استدعاؤه: التقويم حديث"))
        app._refresh_events()                                               # التقويم حديث (<6 ساعات) ← لا احتياط
        A.fetch_ff = lambda *a, **k: FetchResult(pf(SAMPLE), [], 0, False)
        d3 = app._refresh_events()
        self.assertEqual(app.news.source, "forexfactory")
        self.assertEqual(d3, 3600.0)
        self.assertEqual(app._news_fails, 0)


if __name__ == "__main__":
    unittest.main()

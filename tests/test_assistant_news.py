import asyncio, json, os, tempfile, unittest
import pandas as pd
from tests.helpers import cfg, FakeTG
from tests.test_e2e import find_tradable
from tests.test_arabic_private import Resp, FakeSession
from goldbot.analysis.news import parse_feed, fetch_feed, NewsFilter, NewsStatus
from goldbot.ai.assistant import Assistant
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


def mkapp():
    c = cfg(paths__journal=os.path.join(tempfile.mkdtemp(), "j.sqlite"), paths__log_dir=tempfile.mkdtemp(),
            news__cache_path=os.path.join(tempfile.mkdtemp(), "n.json"))
    return c, BotApp(c, tg=FakeTG(), history_provider=SyntheticHistory())


def text_resp(t):
    return Resp(200, {"candidates": [{"content": {"role": "model", "parts": [{"text": t}]}}]})


def call_resp(name, args):
    return Resp(200, {"candidates": [{"content": {"role": "model", "parts": [{"functionCall": {"name": name, "args": args}, "thoughtSignature": "SIG123"}]}}]})


class TestAssistant(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        os.environ["GEMINI_API_KEY"] = "k"
        os.environ.pop("GEMINI_MODEL", None)

    async def test_chart_request_uses_tool_sends_photo_and_keeps_signature(self):
        df, cand = find_tradable()
        c, app = mkapp()
        app.last_cand = cand
        seq = [call_resp("get_chart", {"timeframe": "15m"}), text_resp("هذا شارت 15 دقيقة، الاتجاه صاعد.")]
        s = FakeSession(lambda *a: seq.pop(0))
        app.gemini = GeminiLayer(c, session=s)
        reply, photos = await Assistant(app).answer(1, "ابي صورة الشارت 15 دقيقة")
        self.assertEqual(reply, "هذا شارت 15 دقيقة، الاتجاه صاعد.")
        self.assertEqual(len(photos), 1)
        self.assertEqual(photos[0][:4], b"\x89PNG")
        first, second = s.calls[0]["json"], s.calls[1]["json"]
        self.assertIn("tools", first)
        names = [d["name"] for d in first["tools"][0]["functionDeclarations"]]
        for n in ("get_price", "get_analysis", "get_news", "get_chart", "get_stats", "explain_no_trade"):
            self.assertIn(n, names)
        model_turn = second["contents"][-2]
        self.assertEqual(model_turn["parts"][0]["thoughtSignature"], "SIG123")      # أُعيد كما هو
        last = second["contents"][-1]["parts"]
        self.assertIn("functionResponse", last[0])
        self.assertTrue(any("inlineData" in p for p in last))                       # Gemini يرى الصورة أيضًا
        self.assertIn("models/gemini-3.8-flash:generateContent", s.calls[0]["url"])

    async def test_news_and_price_tools(self):
        df, cand = find_tradable()
        c, app = mkapp()
        app.last_cand = cand
        app.news.apply_feed(parse_feed(SAMPLE), pd.Timestamp("2026-10-01", tz="UTC"))
        a = Assistant(app)
        res, imgs = a.run_tool("get_news", {"count": 2})
        self.assertEqual(res["upcoming"][0]["name"], "CPI m/m")
        self.assertEqual(imgs, [])
        self.assertIn("لم يصل", a.run_tool("get_price", {})[0])
        self.assertIn("error", a.run_tool("nope", {})[0])
        res, imgs = a.run_tool("get_chart", {"timeframe": "1H"})
        self.assertEqual(len(imgs), 1)

    async def test_user_photo_and_retry_without_images_on_400(self):
        c, app = mkapp()
        def responder(url, d, j):
            return Resp(400) if "inlineData" in json.dumps(j) else text_resp("لا أرى الصورة لكن السعر كذا")
        s = FakeSession(responder)
        app.gemini = GeminiLayer(c, session=s)
        reply, photos = await Assistant(app).answer(1, "حلل", image=b"\xff\xd8jpegbytes")
        self.assertEqual(reply, "لا أرى الصورة لكن السعر كذا")
        self.assertEqual(len(s.calls), 2)
        self.assertIn("inlineData", json.dumps(s.calls[0]["json"]))
        self.assertNotIn("inlineData", json.dumps(s.calls[1]["json"]))

    async def test_memory_between_turns(self):
        c, app = mkapp()
        s = FakeSession(lambda *a: text_resp("رد"))
        app.gemini = GeminiLayer(c, session=s)
        a = Assistant(app)
        await a.answer(7, "السؤال الأول")
        await a.answer(7, "وماذا عن الثاني؟")
        texts = json.dumps(s.calls[1]["json"]["contents"], ensure_ascii=False)
        self.assertIn("السؤال الأول", texts)

    async def test_adapter_mode_gets_live_data_in_prompt(self):
        c, app = mkapp()
        got = []
        app.gemini.adapter = lambda p: got.append(p) or "جواب من دالتي"
        reply, photos = await Assistant(app).answer(1, "كم السعر؟")
        self.assertEqual(reply, "جواب من دالتي")
        self.assertIn("حالة النظام", got[0])
        self.assertIn("كم السعر؟", got[0])

    async def test_ai_failure_gives_arabic_fallback(self):
        c, app = mkapp()
        app.gemini = GeminiLayer(c, session=FakeSession(lambda *a: Resp(500)))
        reply, _ = await Assistant(app).answer(1, "مرحبا")
        self.assertIn("تعذّر", reply)
        self.assertIn("/status", reply)

    async def test_signal_review_sends_chart_image(self):
        c, app = mkapp()
        s = FakeSession(lambda *a: text_resp('{"decision":"BUY","reason":"الشارت يؤكد"}'))
        g = GeminiLayer(c, session=s)
        d = await g.decide("card", "BUY", image=b"\x89PNGfake")
        self.assertEqual(d.decision, "BUY")
        parts = s.calls[0]["json"]["contents"][0]["parts"]
        self.assertTrue(any("inlineData" in p for p in parts))
        self.assertIn("الشارت", parts[0]["text"])


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
    async def test_admin_text_and_photo_go_to_ai_strangers_ignored(self):
        tg = TgAI()
        bot = CommandBot(tg, {"/price": lambda a: "PRICE"})
        calls = []

        async def ai(chat, text, img):
            calls.append((chat, text, img))
            return "رد الذكاء", [b"PNGDATA"]
        bot.ai = ai
        U = lambda uid, **m: {"update_id": 1, "message": {"from": {"id": uid}, "chat": {"id": uid}, **m}}
        await bot.handle_update(U(1, text="كم سعر الذهب؟"))
        await bot.handle_update(U(1, caption="حلل", photo=[{"file_id": "a", "file_size": 1}]))
        await bot.handle_update(U(999, text="مرحبا"))
        await bot.handle_update(U(1, text="/price"))
        self.assertEqual([c[1] for c in calls], ["كم سعر الذهب؟", "حلل"])
        self.assertEqual(calls[1][2], b"IMG")
        self.assertEqual(tg.dl, 1)
        self.assertEqual(tg.photos_sent, [(1, 7), (1, 7)])
        self.assertIn((1, "رد الذكاء"), tg.private)
        self.assertIn((1, "PRICE"), tg.private)
        self.assertNotIn(999, [c[0] for c in calls])


if __name__ == "__main__":
    unittest.main()

import asyncio, os, unittest
from unittest import mock
import pandas as pd

from goldbot.assist import deepseek as D
from goldbot.assist.deepseek import DeepSeek
from goldbot.notify.telegram import CommandBot
from goldbot.commands import build_handlers
from tests.helpers import cfg, mkapp
from tests.test_e2e import find_tradable   # noqa  (مولّد مرشح صفقة قابل للإرسال)

KEY = "test-key-not-real"


def resp(text="شرح تجريبي", code=200):
    m = mock.Mock(status_code=code)
    m.json.return_value = {"choices": [{"message": {"content": text}}]}
    return m


class TestClient(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": KEY})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_request_shape_and_model(self):
        with mock.patch.object(D.requests, "post", return_value=resp()) as post:
            out = DeepSeek(cfg()).chat("sys", "user")
        self.assertEqual(out, "شرح تجريبي")
        a, kw = post.call_args
        self.assertEqual(a[0], "https://api.deepseek.com/chat/completions")
        self.assertEqual(kw["json"]["model"], "deepseek-v4-flash")
        self.assertEqual(kw["headers"]["Authorization"], f"Bearer {KEY}")
        self.assertFalse(kw["json"]["stream"])

    def test_model_env_override(self):
        with mock.patch.dict(os.environ, {"DEEPSEEK_MODEL": "deepseek-v4-pro"}):
            self.assertEqual(DeepSeek(cfg()).model, "deepseek-v4-pro")

    def test_no_key_means_silent_and_no_network(self):
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}), mock.patch.object(D.requests, "post") as post:
            ds = DeepSeek(cfg())
            self.assertFalse(ds.enabled)
            self.assertIsNone(ds.chat("s", "u"))
            self.assertIsNone(ds.explain_signal("x"))
        post.assert_not_called()

    def test_failures_return_none_never_raise(self):
        for side in (resp(code=401), resp(code=429), resp(code=500), Exception("boom"), resp(text="")):
            with mock.patch.object(D.requests, "post", side_effect=[side] if isinstance(side, Exception) else None,
                                   return_value=None if isinstance(side, Exception) else side):
                self.assertIsNone(DeepSeek(cfg()).chat("s", "u"))

    def test_malformed_json_is_handled(self):
        bad = mock.Mock(status_code=200)
        bad.json.return_value = {"nope": 1}
        with mock.patch.object(D.requests, "post", return_value=bad):
            self.assertIsNone(DeepSeek(cfg()).chat("s", "u"))

    def test_daily_cap(self):
        ds = DeepSeek(cfg(assist__max_calls_per_day=2))
        with mock.patch.object(D.requests, "post", return_value=resp()) as post:
            for _ in range(5):
                ds.chat("s", "u")
        self.assertEqual(post.call_count, 2)

    def test_key_never_logged(self):
        with self.assertLogs("assist", level="WARNING") as lg, mock.patch.object(D.requests, "post", return_value=resp(code=401)):
            DeepSeek(cfg()).chat("s", "u")
        self.assertNotIn(KEY, "\n".join(lg.output))

    def test_prompts_forbid_changing_levels(self):
        self.assertIn("ممنوع تغيير مستويات", D.SYS_EXPLAIN)
        self.assertIn("ممنوع اختراع", D.SYS_ASK)

    def test_ask_rate_limited_per_chat(self):
        ds = DeepSeek(cfg())
        with mock.patch.object(D.requests, "post", return_value=resp("جواب")) as post:
            first = ds.ask("سؤال", "ctx", chat=1)
            second = ds.ask("سؤال", "ctx", chat=1)
        self.assertIn("جواب", first)
        self.assertIn("بضع ثوانٍ", second)
        self.assertEqual(post.call_count, 1)


class TestIntegration(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": KEY})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    async def test_signal_is_sent_first_then_explained_and_levels_untouched(self):
        c, app = mkapp()
        _, cand = find_tradable()
        plan_before = (cand.best.plan.entry, cand.best.plan.sl, cand.best.plan.tp1, cand.best.plan.tp2)
        with mock.patch.object(D.requests, "post", return_value=resp("لأن الاتجاه والسيولة متفقان")) as post:
            await app.emit_signal(cand)
            await asyncio.gather(*list(app._bg))
        self.assertTrue(app.tg.channel)                              # الإشارة نفسها وصلت
        self.assertEqual(app.journal.stats()["total"], 1)
        self.assertEqual(plan_before, (cand.best.plan.entry, cand.best.plan.sl, cand.best.plan.tp1, cand.best.plan.tp2))
        queued = [app.notify_q.get_nowait() for _ in range(app.notify_q.qsize())]
        self.assertTrue(any("شرح الإشارة" in m and "DeepSeek" in m for m in queued))
        sent_prompt = post.call_args.kwargs["json"]["messages"][1]["content"]
        self.assertIn(str(cand.best.plan.entry), sent_prompt)         # الأرقام من البوت لا من النموذج

    async def test_deepseek_down_does_not_break_signal(self):
        c, app = mkapp()
        _, cand = find_tradable()
        with mock.patch.object(D.requests, "post", side_effect=Exception("down")):
            await app.emit_signal(cand)
            await asyncio.gather(*list(app._bg))
        self.assertTrue(app.tg.channel)
        self.assertEqual(app.journal.stats()["total"], 1)
        self.assertEqual(app.notify_q.qsize(), 0)

    def test_free_text_goes_to_assistant_with_bot_context(self):
        c, app = mkapp()
        bot = CommandBot(app.tg, build_handlers(app))
        bot.free_text = app._free_text
        with mock.patch.object(D.requests, "post", return_value=resp("السوق هادئ")) as post:
            bot.process_update({"update_id": 1, "message": {"from": {"id": 1}, "chat": {"id": 1}, "text": "وش وضع السوق؟"}})
        self.assertIn("السوق هادئ", app.tg.private[-1][1])
        ctx = post.call_args.kwargs["json"]["messages"][1]["content"]
        self.assertIn("[الإحصائيات]", ctx)
        self.assertIn("وش وضع السوق؟", ctx)

    def test_free_text_without_key_falls_back_to_hint(self):
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}):
            c, app = mkapp()
            bot = CommandBot(app.tg, build_handlers(app))
            bot.free_text = app._free_text
            bot.process_update({"update_id": 1, "message": {"from": {"id": 1}, "chat": {"id": 1}, "text": "مرحبا"}})
        self.assertIn("الأزرار", app.tg.private[-1][1])

    def test_buttons_never_go_to_assistant(self):
        c, app = mkapp()
        bot = CommandBot(app.tg, build_handlers(app))
        bot.free_text = app._free_text
        with mock.patch.object(D.requests, "post") as post:
            bot.process_update({"update_id": 1, "message": {"from": {"id": 1}, "chat": {"id": 1}, "text": "📊 الإحصائيات"}})
            bot.process_update({"update_id": 2, "message": {"from": {"id": 1}, "chat": {"id": 1}, "text": "/status"}})
        post.assert_not_called()

    def test_summary_numbers_come_from_journal_and_survive_without_ai(self):
        c, app = mkapp()
        now = pd.Timestamp.now(tz="UTC")
        self.assertIsNone(app.build_summary(now))                     # لا إشارات → لا ملخص
        app.journal.add_signal(timestamp=now.isoformat(), symbol="XAUUSD", direction="BUY", entry=2000.0, sl=1995.0,
                               tp1=2006.0, tp2=2012.0, strategy="Breakout", session="London", score=70,
                               historical_probability=None, prob_n=0, risk_level="Low", reason="x", fingerprint={},
                               rr1=1.2, rr2=2.4)
        res = app.build_summary(now)
        self.assertIn("1 إشارة", res[0])
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}):
            self.assertIsNone(app.assist.comment_summary(res[1]))     # بدون مفتاح: أرقام فقط، بلا خطأ

    def test_status_reports_assistant(self):
        c, app = mkapp()
        self.assertIn("المساعد:", build_handlers(app)["/status"]([]))


if __name__ == "__main__":
    unittest.main()

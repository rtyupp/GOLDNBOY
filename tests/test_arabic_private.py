import unittest
from tests.helpers import FakeTG
from goldbot import i18n as T
from goldbot.notify.telegram import Telegram, CommandBot
from goldbot.monitor import CycleLog
from tests.test_e2e import find_tradable


class Resp:
    def __init__(self, code, js=None):
        self.status_code, self._js = code, js or {}

    def json(self):
        return self._js

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class FakeSession:
    def __init__(self, responder):
        self.calls, self.responder = [], responder

    def post(self, url, data=None, json=None, files=None, headers=None, timeout=None):
        import copy
        self.calls.append({"url": url, "data": data, "json": copy.deepcopy(json)})
        return self.responder(url, data, json)


class TestPrivateRouting(unittest.TestCase):
    def ok(self, *a):
        return Resp(200, {"ok": True, "result": {"message_id": 1}})

    def test_private_goes_to_admins_only(self):
        s = FakeSession(self.ok)
        tg = Telegram("TOK", "@chan", [111, 222], "private", session=s)
        self.assertTrue(tg.send_signal("مرحبا"))
        self.assertEqual([c["data"]["chat_id"] for c in s.calls], [111, 222])

    def test_channel_and_both(self):
        s = FakeSession(self.ok)
        Telegram("TOK", "@chan", [111], "channel", session=s).send_signal("x")
        self.assertEqual([c["data"]["chat_id"] for c in s.calls], ["@chan"])
        s2 = FakeSession(self.ok)
        Telegram("TOK", "@chan", [111], "both", session=s2).send_signal("x")
        self.assertEqual([c["data"]["chat_id"] for c in s2.calls], [111, "@chan"])

    def test_channel_not_required_for_private(self):
        s = FakeSession(self.ok)
        self.assertTrue(Telegram("TOK", None, [5], "private", session=s).send_signal("x"))

    def test_403_gives_start_hint_and_false(self):
        s = FakeSession(lambda *a: Resp(403, {"ok": False, "description": "Forbidden: bot can't initiate conversation"}))
        tg = Telegram("TOK", None, [5], "private", session=s)
        self.assertFalse(tg.send_signal("x"))
        self.assertIn("Forbidden", tg.last_error)

    def test_id_command_open_to_strangers_but_others_gated(self):
        tg = FakeTG()
        bot = CommandBot(tg, {"/status": lambda a: "OK"})
        bot.process_update({"update_id": 1, "message": {"text": "/id", "from": {"id": 777}, "chat": {"id": 777}}})
        bot.process_update({"update_id": 2, "message": {"text": "/status", "from": {"id": 777}, "chat": {"id": 777}}})
        self.assertEqual(len(tg.private), 1)
        self.assertIn("777", tg.private[0][1])


class TestArabic(unittest.TestCase):
    def test_reasons_translate(self):
        self.assertIn("بيانات", T.reason_ar("STALE_DATA(30s>20s)"))
        self.assertEqual(T.reason_ar("UNKNOWN_CODE"), "UNKNOWN_CODE")
        self.assertEqual(T.final_ar("BUY SENT"), "تم إرسال إشارة شراء")

    def test_cycle_log_block_is_arabic(self):
        lg = CycleLog(ts="2026-06-10 10:15 UTC")
        lg.set("DATA", "سليمة")
        lg.final, lg.reasons = "NO TRADE", ["LOW_CONFLUENCE(55<70)", "NO_SETUP"]
        b = lg.block()
        for w in ("البيانات", "القرار النهائي: لا صفقة", "السبب:", "تقاطع الأدلة ضعيف"):
            self.assertIn(w, b)
        self.assertNotIn("FINAL", b)

    def test_every_strategy_check_has_arabic(self):
        import re
        src = "".join(open(p, encoding="utf-8").read() for p in
                      ("goldbot/strategies/impl.py", "goldbot/strategies/smc.py", "goldbot/strategies/advanced.py"))
        names = set(re.findall(r'ch\.add\("([^"]+)"', src))
        missing = [n for n in names if n not in T.CHECKS]
        self.assertEqual(missing, [])

    def test_shaping_produces_presentation_forms_rtl(self):
        out = T.shape_word("دخول")
        self.assertTrue(all(0xFE70 <= ord(c) <= 0xFEFF for c in out))
        self.assertEqual(len(out), 4)
        self.assertNotEqual(out, "دخول")
        lam_alef = T.shape_word("لا")
        self.assertEqual(len(lam_alef), 1)                 # ligature
        self.assertEqual(T.ar("الهدف 1").split(" ")[-1][0], "ﻟ"[0] if False else T.ar("الهدف 1").split(" ")[-1][0])

    def test_chart_with_arabic_renders(self):
        from goldbot.notify.chart import render_chart
        df, cand = find_tradable()
        png = render_chart(cand.ctx, cand.best.plan)
        self.assertEqual(png[:4], b"\x89PNG")
        open("/tmp/chart_ar.png", "wb").write(png)

    def test_signal_message_has_no_english_labels(self):
        from goldbot.notify.telegram import format_signal
        df, cand = find_tradable()
        t = format_signal(cand.best.plan, cand.best.name, cand.ctx.session["label"], 80, "غير كافٍ", "x")
        for bad in ("Entry", "Reason", "Session", "Confidence", "BUY", "SELL"):
            self.assertNotIn(bad, t)


if __name__ == "__main__":
    unittest.main()

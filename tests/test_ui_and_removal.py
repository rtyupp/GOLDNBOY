import json, os, re, unittest
from goldbot.notify.telegram import (CommandBot, Telegram, BUTTON_COMMANDS, MAIN_ROWS, MORE_ROWS, WELCOME,
                                     reply_keyboard, is_private_chat)
from goldbot.commands import build_handlers
from tests.helpers import mkapp, FakeTG


def upd(text, uid=1, chat=1):
    return {"update_id": 1, "message": {"from": {"id": uid}, "chat": {"id": chat}, "text": text}}


class TestKeyboard(unittest.TestCase):
    def test_reply_keyboard_is_bottom_persistent(self):
        k = json.loads(reply_keyboard("main"))
        self.assertTrue(k["is_persistent"] and k["resize_keyboard"])
        self.assertFalse(k["one_time_keyboard"])
        self.assertEqual([[b["text"] for b in row] for row in k["keyboard"]], MAIN_ROWS)
        self.assertNotIn("inline_keyboard", k)

    def test_rows_are_short_and_labels_unique(self):
        labels = [t for row in MAIN_ROWS + MORE_ROWS for t in row]
        self.assertEqual(len(labels), len(set(labels)))
        self.assertTrue(all(len(r) <= 2 for r in MAIN_ROWS + MORE_ROWS))

    def test_every_button_has_a_working_handler(self):
        c, app = mkapp()
        h = build_handlers(app)
        for label, cmd in BUTTON_COMMANDS.items():
            self.assertIn(cmd, h, label)

    def test_every_button_returns_a_reply_and_keeps_keyboard(self):
        c, app = mkapp()
        tg = FakeTG()
        bot = CommandBot(tg, build_handlers(app))
        for label in BUTTON_COMMANDS:
            tg.private.clear(); tg.menus.clear()
            if label == "🧪 إعادة بناء السجل":
                continue            # يشغّل Backtest في الخلفية: يُختبر منفصلًا
            bot.process_update(upd(label))
            self.assertEqual(len(tg.private), 1, label)
            self.assertTrue(tg.private[0][1].strip(), label)
            self.assertNotIn("خطأ", tg.private[0][1][:20], label)
            self.assertIn(tg.menus[0], ("main", "more"), label)        # اللوحة مرفقة مع كل رد

    def test_navigation_between_sections(self):
        c, app = mkapp()
        tg = FakeTG()
        bot = CommandBot(tg, build_handlers(app))
        bot.process_update(upd("⚙️ المزيد"))
        self.assertEqual(tg.menus[-1], "more")
        bot.process_update(upd("🟢 حالة النظام"))
        self.assertEqual(tg.menus[-1], "more")                          # تبقى في القسم نفسه
        bot.process_update(upd("🔙 القائمة الرئيسية"))
        self.assertEqual(tg.menus[-1], "main")
        bot.process_update(upd("💰 السعر اللحظي"))
        self.assertEqual(tg.menus[-1], "main")

    def test_button_matches_even_without_emoji(self):
        c, app = mkapp()
        tg = FakeTG()
        bot = CommandBot(tg, build_handlers(app))
        bot.process_update(upd("السعر اللحظي"))
        self.assertIn("سعر", tg.private[-1][1])

    def test_free_text_gets_hint_not_ai(self):
        c, app = mkapp()
        tg = FakeTG()
        bot = CommandBot(tg, build_handlers(app))
        bot.process_update(upd("كم سعر الذهب الآن؟"))
        self.assertIn("الأزرار", tg.private[-1][1])

    def test_strangers_ignored_but_id_open(self):
        c, app = mkapp()
        tg = FakeTG()
        bot = CommandBot(tg, build_handlers(app))
        bot.process_update(upd("/start", uid=99, chat=99))
        self.assertEqual(tg.private, [])
        bot.process_update(upd("/id", uid=99, chat=99))
        self.assertIn("99", tg.private[-1][1])

    def test_train_button_starts_without_crash(self):
        c, app = mkapp()
        h = build_handlers(app)
        app.bootstrap.run_async = lambda *a, **k: None if hasattr(app.bootstrap, "run_async") else None
        self.assertIn("/train", h)


class TestWelcome(unittest.TestCase):
    def test_exact_text(self):
        self.assertIn("ارحبو تراحيب المطر", WELCOME)
        self.assertIn("بوت GOLDNBOY لإشارات تداول الذهب XAUUSD، يعتمد على الأسعار اللحظية والاستراتيجيات والمؤشرات الفنية "
                      "لتحليل السوق وتحديد فرص التداول المحتملة.", WELCOME)
        self.assertTrue(WELCOME.rstrip().endswith("by: @QUOP9"))
        for bad in ("ذكاء", "Gemini", "أخبار"):
            self.assertNotIn(bad, WELCOME)

    def test_start_sends_welcome_with_main_keyboard(self):
        c, app = mkapp()
        tg = FakeTG()
        bot = CommandBot(tg, build_handlers(app))
        bot.process_update(upd("/more"))
        bot.process_update(upd("⚙️ المزيد"))
        bot.process_update(upd("/start"))
        self.assertEqual(tg.private[-1][1], WELCOME)
        self.assertEqual(tg.menus[-1], "main")


class TestTelegramPayload(unittest.TestCase):
    def _tg(self):
        t = Telegram("123:abc", None, [1])
        calls = []
        t._call = lambda method, **kw: calls.append((method, kw)) or {"ok": 1}
        return t, calls

    def test_private_chat_gets_reply_markup(self):
        t, calls = self._tg()
        t.send_message(5, "x", menu="main")
        self.assertIn("reply_markup", calls[0][1])
        self.assertTrue(json.loads(calls[0][1]["reply_markup"])["is_persistent"])

    def test_channel_never_gets_keyboard(self):
        t, calls = self._tg()
        for ch in ("@mychannel", -1001234567890):
            t.send_message(ch, "x", menu="main")
        self.assertTrue(all("reply_markup" not in c[1] for c in calls))
        self.assertFalse(is_private_chat("@c") or is_private_chat(-100))

    def test_no_menu_leaves_keyboard_untouched(self):
        t, calls = self._tg()
        t.send_message(5, "signal")
        self.assertNotIn("reply_markup", calls[0][1])


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# لا ذكاء اصطناعي يقرر، ولا نماذج تعلّم آلي في مسار القرار. (فلتر الأخبار صار ميزة مقصودة — انظر TestNewsFilter.)
FORBIDDEN = re.compile(r"gemini|generativelanguage|GEMINI_API_KEY|goldbot\.ai|goldbot\.ml|lightgbm|xgboost|sklearn|scikit", re.I)


class TestAiNewsRemoved(unittest.TestCase):
    def test_modules_deleted(self):
        for rel in ("goldbot/ai", "goldbot/ml", "goldbot/analysis/news_ai.py", "data/events.csv"):
            self.assertFalse(os.path.exists(os.path.join(ROOT, rel)), rel)

    def test_no_references_anywhere(self):
        hits = []
        for base, dirs, files in os.walk(ROOT):
            # vendor/dukascopy-node is third-party source; its instrument metadata
            # may contain legitimate company names matching the product guard.
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "reports", "logs", "vendor")]
            for f in files:
                if f.endswith((".pyc", ".png", ".zip", ".sqlite")) or f == "test_ui_and_removal.py":
                    continue
                p = os.path.join(base, f)
                with open(p, encoding="utf-8", errors="ignore") as fh:
                    for n, line in enumerate(fh, 1):
                        if FORBIDDEN.search(line):
                            hits.append(f"{os.path.relpath(p, ROOT)}:{n}: {line.strip()[:80]}")
        self.assertEqual(hits, [])

    def test_env_and_requirements_clean(self):
        env = open(os.path.join(ROOT, ".env.example"), encoding="utf-8").read()
        req = open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8").read()
        for bad in ("GEMINI", "EVENTS", "OPENAI", "ANTHROPIC"):
            self.assertNotIn(bad, env)
        self.assertNotIn("scikit", req.lower())
        for line in env.splitlines():                        # لا قيم سرية حقيقية
            if "=" in line and not line.startswith("#"):
                self.assertEqual(line.split("=", 1)[1].strip() in ("", "paper", "live"), True, line)
        self.assertIn("DEEPSEEK_API_KEY=", env)

    def test_no_secret_like_strings_in_code(self):
        pat = re.compile(r"\d{8,10}:[A-Za-z0-9_-]{30,}|AIza[0-9A-Za-z_-]{30,}|sk-[A-Za-z0-9]{20,}")
        for base, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
            for f in files:
                if f.endswith((".py", ".yaml", ".yml", ".md", ".example", ".sh", ".txt")):
                    t = open(os.path.join(base, f), encoding="utf-8", errors="ignore").read()
                    self.assertIsNone(pat.search(t), os.path.join(base, f))

    def test_system_runs_without_any_ai_or_news_setting(self):
        for k in list(os.environ):
            if "GEMINI" in k or "EVENTS" in k:
                os.environ.pop(k)
        c, app = mkapp()
        self.assertTrue(len(app.pipeline.strategies) >= 7)
        self.assertFalse(hasattr(app, "gemini") or hasattr(app, "ml"))


if __name__ == "__main__":
    unittest.main()

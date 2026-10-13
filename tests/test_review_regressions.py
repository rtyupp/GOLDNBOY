"""اختبارات انحدار لمشاكل المتانة التي أُصلحت في مراجعة GOLDNBOY.

كل اختبار يقفل سلوكاً كان معطوباً (أو هشاً) قبل الإصلاح، حتى لا يعود بصمت.
"""
import contextlib
import io
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import pandas as pd

from tests.helpers import cfg, mkapp
from goldbot.tracking.journal import Journal
from goldbot.tracking.tracker import TradeTracker
from goldbot.commands import build_handlers


class TestHtfLevelsGuard(unittest.TestCase):
    """levels.htf_levels يجب ألا ينهار عند غياب إطار اليومي (None)."""

    def test_none_returns_empty_dict(self):
        from goldbot.analysis import levels as lv
        self.assertEqual(lv.htf_levels(None), {})

    def test_empty_frame_returns_empty_dict(self):
        from goldbot.analysis import levels as lv
        empty = pd.DataFrame(columns=["high", "low"], index=pd.DatetimeIndex([], tz="UTC"))
        self.assertEqual(lv.htf_levels(empty), {})

    def test_analyzer_without_daily_frame_still_analyzes(self):
        """المسار الفعلي: تحليل بلا شمعة يومية يجب ألا يرمي AttributeError."""
        from goldbot.analysis.analyzer import Analyzer
        from goldbot.core.market_data import MarketData
        from goldbot.core.timeframes import ALL_TFS
        from goldbot.providers.synthetic import make_synthetic_1m

        c = cfg()
        df = make_synthetic_1m(days=20, seed=2)
        md = MarketData()
        md.load_history(df)
        as_of = df.index[-1] + pd.Timedelta(minutes=1)
        frames = md.frames(as_of, tfs=[tf for tf in ALL_TFS if tf != "1D"])
        px = float(df["close"].iloc[-1])
        ctx, why = Analyzer(c).analyze(frames, px, px + 0.3, as_of)
        self.assertEqual(why, "OK")
        self.assertIsNotNone(ctx)
        self.assertNotIn("PDH", ctx.levels)      # بلا يومي لا توجد مستويات يومية


class TestBacktestProgressLambda(unittest.TestCase):
    """progress في cmd_backtest كان يطبع آخر قيمة للفريم بسبب الربط المتأخر (late binding)."""

    def test_each_timeframe_reports_its_own_label(self):
        import goldbot.__main__ as main
        from goldbot.backtest import engine

        captured = []

        def fake_run_backtest(base, cfg, tf, step=1, progress=None, **kw):
            captured.append(progress)          # نحتفظ بالدالة ثم نناديها لاحقاً كما يفعل المحرك الحقيقي
            return {"eval_tf": tf, "strategies": {}, "books": {}}

        base = pd.DataFrame(
            {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1.0]},
            index=pd.DatetimeIndex(["2026-06-01"], tz="UTC"))

        a = SimpleNamespace(tf="all", days=1, step=None, store=False)
        c = cfg(paths__journal=os.path.join(tempfile.mkdtemp(), "j.sqlite"), paths__log_dir=tempfile.mkdtemp())
        with mock.patch.object(main, "_history_1m", return_value=base), \
                mock.patch.object(engine, "run_backtest", fake_run_backtest), \
                mock.patch.object(engine, "save_report", lambda *x, **k: "r.json"), \
                contextlib.redirect_stdout(io.StringIO()):
            main.cmd_backtest(c, a)

        # ننادي الدوال بعد انتهاء الحلقة: هنا يظهر الربط المتأخر لو كان موجوداً
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            for p in captured:
                p(3, 9)
            out = buf.getvalue()
        # يجب أن يطبع كل فريم ملصقه الصحيح لا الفريم الأخير مراراً
        self.assertIn("1m: 3/9", out)
        self.assertIn("5m: 3/9", out)
        self.assertIn("15m: 3/9", out)
        self.assertNotEqual(out.count("15m: 3/9"), len(captured))


class TestFreeTextLogsErrors(unittest.TestCase):
    """_free_text كان يكتم أخطاء الـ handlers بصمت (except: pass)."""

    def test_handler_exception_is_logged_not_swallowed(self):
        _, app = mkapp()
        app.assist = mock.Mock()
        app.assist.enabled = True
        app.assist.ask.return_value = "رد"
        boom = mock.Mock(side_effect=RuntimeError("boom"))
        # نُفسد أمراً واحداً فقط من أوامر السياق
        original = app._handlers["/stats"]
        app._handlers["/stats"] = boom
        try:
            with self.assertLogs("app", level="WARNING") as cap:
                out = app._free_text("سؤال", chat=1)
        finally:
            app._handlers["/stats"] = original
        self.assertEqual(out, "رد")
        self.assertTrue(any("stats" in m or "الإحصائيات" in m for m in cap.output))
        app.assist.ask.assert_called_once()


class TestUserAgentOverride(unittest.TestCase):
    """setdefault على جلسة requests لا يعمل: الجلسة تحمل UA افتراضي python-requests فيحجب Dukascopy."""

    def test_default_session_user_agent_is_overridden(self):
        import requests

        from goldbot.providers.dukascopy import DukascopyHistory
        d = DukascopyHistory({}, session=requests.Session())
        self.assertNotIn("python-requests", d.s.headers["User-Agent"].lower())

    def test_custom_user_agent_is_kept(self):
        import requests

        from goldbot.providers.dukascopy import DukascopyHistory
        s = requests.Session()
        s.headers["User-Agent"] = "my-custom-agent"
        d = DukascopyHistory({}, session=s)
        self.assertEqual(d.s.headers["User-Agent"], "my-custom-agent")


class TestBootstrapWindow(unittest.TestCase):
    """التهيئة الذاتية على الذاكرة القليلة كانت تبني 7 أيام → المحلل يحتاج 60 شمعة 4H ≈ 10 أيام فينتج 0 صفقة."""

    def test_plan_never_schedules_a_zero_trade_window(self):
        from goldbot import resources
        c = cfg()
        with mock.patch.object(resources, "limit_mb", return_value=512), \
                mock.patch.object(resources, "usage_mb", return_value=150):
            plan = resources.plan_bootstrap(c)
        self.assertTrue(plan["run"])
        self.assertGreaterEqual(plan["days"], 16)      # 60 شمعة 4H ≈ 10 أيام + إحماء
        self.assertIn("5m", plan["tfs"])               # 15m وحده لا يصل لعتبة الالتقاء في مدى Yahoo

    def test_unreachable_min_trades_no_longer_loops(self):
        c = cfg()
        min_trades = int(c.get("bootstrap.min_trades", 150))
        # 28 يومًا (حد 1m في Yahoo) تُنتج نحو 30 صفقة؛ العتبة يجب أن تكون قابلة للتحقيق وإلا أُعيد البناء أبدًا
        self.assertLessEqual(min_trades, 30)


class TestBacktestWarmupScaling(unittest.TestCase):
    """الإحماء الثابت (12 يومًا) كان يبتلع نافذة قصيرة كاملة فلا يُقيَّم أي شمعة → 0 صفقات."""

    def test_short_window_still_evaluates_some_bars(self):
        from goldbot.backtest.engine import run_backtest
        from goldbot.providers.synthetic import make_synthetic_1m
        c = cfg()
        df = make_synthetic_1m(days=16, seed=5)
        res = run_backtest(df, c, "5m", warmup_days=12)
        self.assertGreater(res["evals"], 0)            # لا يمكن أن تُستهلك كل النافذة إحماءً


class TestYahooHistory(unittest.TestCase):
    """مزوّد Yahoo البديل: يحوّل رد chart إلى OHLCV ويُعيد التجميع بمفاتيح الأطر الصحيحة."""

    class Resp:
        def __init__(self, payload, code=200):
            self.status_code, self._p = code, payload

        def json(self):
            return self._p

    class Sess:
        def __init__(self, payload):
            self.payload, self.headers = payload, {}

        def get(self, url, params=None, timeout=None):
            return TestYahooHistory.Resp(self.payload)

    @staticmethod
    def _window():
        # نستخدم بيانات قريبة من الآن لأن Yahoo يقصّ النوافذ الأقدم من نطاق الاحتفاظ
        start = pd.Timestamp.now(tz="UTC").floor("h") - pd.Timedelta(hours=3)
        return start, start + pd.Timedelta(minutes=90)

    @classmethod
    def _payload(cls, mins=90):
        base, _ = cls._window()
        ts = [int((base + pd.Timedelta(minutes=i)).timestamp()) for i in range(mins)]
        o = [2000.0 + i * 0.1 for i in range(mins)]
        return {"chart": {"error": None, "result": [{
            "timestamp": ts,
            "indicators": {"quote": [{"open": o, "high": [x + 1 for x in o],
                                      "low": [x - 1 for x in o], "close": [x + 0.5 for x in o],
                                      "volume": [0] * mins}]}}]}}

    def test_parses_chart_payload_to_ohlcv(self):
        from goldbot.providers.yahoo import YahooHistory
        start, end = self._window()
        p = YahooHistory({}, "XAUUSD", session=self.Sess(self._payload()))
        df = p.fetch("1m", start, end)
        self.assertEqual(len(df), 90)
        self.assertEqual(list(df.columns), ["open", "high", "low", "close", "volume"])
        self.assertAlmostEqual(float(df["high"].iloc[0]), 2001.0)

    def test_resample_uses_valid_timeframe_keys(self):
        """الخطأ القديم: المفاتيح 5min/15min/1h لا وجود لها في TF_RULES فينهار resample_ohlcv."""
        from goldbot.providers.yahoo import YahooHistory
        start, end = self._window()
        p = YahooHistory({}, "XAUUSD", session=self.Sess(self._payload()))
        for itv in ("5m", "15m", "30m", "1h"):
            df = p.fetch(itv, start, end)
            self.assertFalse(df.empty, itv)
            self.assertEqual(list(df.columns), ["open", "high", "low", "close", "volume"])

    def test_old_window_is_skipped_not_fatal(self):
        """نافذة أقدم من احتفاظ Yahoo تُتخطّى بدل رمي استثناء يُفشل كل التحميل."""
        from goldbot.providers.yahoo import YahooHistory
        err = {"chart": {"error": {"code": "Unprocessable Entity"}, "result": None}}
        p = YahooHistory({}, "XAUUSD", session=self.Sess(err))
        df = p.fetch("1m", pd.Timestamp("2020-01-01", tz="UTC"), pd.Timestamp("2020-01-02", tz="UTC"))
        self.assertTrue(df.empty)


class TestScalpMode(unittest.TestCase):
    """وضع السكالب: خروج زمني سريع + ملف وضع يحمل عتبات أوسع."""

    def _trade(self, j, ts="2026-06-01T10:00:00+00:00"):
        return j.add_signal(timestamp=ts, symbol="XAUUSD", direction="BUY", entry=2650.0, sl=2645.0,
                            tp1=2656.0, tp2=2662.0, strategy="Liquidity Sweep", session="New York", score=80,
                            historical_probability=None, prob_n=0, risk_level="Low", reason="x",
                            fingerprint={"setup": "Liquidity Sweep", "direction": "BUY", "session": "New York",
                                         "htf_bias": "bull", "structure": "BULLISH/BOS", "atr_bucket": "mid",
                                         "rsi_bucket": "50-65", "liquidity": "sweep", "regime": "BULLISH"},
                            rr1=1.2, rr2=2.4)

    def test_scalp_time_exit_closes_open_trade(self):
        j = Journal(os.path.join(tempfile.mkdtemp(), "j.sqlite"))
        sid = self._trade(j)
        msgs = []
        tr = TradeTracker(j, msgs.append, cfg(tracking__scalp_max_minutes=45))
        tr.check_expiry(pd.Timestamp("2026-06-01T10:50Z"), 2652.0, 2652.3)   # بعد 50 دقيقة (≥45) وسعر +0.4R
        self.assertEqual(j.get(sid)["result"], "EXPIRED")
        self.assertTrue(any("خروج سكالب" in m for m in msgs))

    def test_scalp_exit_is_disabled_by_default(self):
        j = Journal(os.path.join(tempfile.mkdtemp(), "j.sqlite"))
        sid = self._trade(j)
        tr = TradeTracker(j, [], cfg())                                     # scalp_max_minutes=0 افتراضيًا
        tr.check_expiry(pd.Timestamp("2026-06-01T10:50Z"), 2652.0, 2652.3)
        self.assertEqual(j.get(sid)["status"], "OPEN")

    def test_scalp_profile_widens_thresholds(self):
        from goldbot.config import load_config
        c = load_config("config/config.yaml", profile="scalp")
        self.assertEqual(c.get("_profile"), "scalp")
        self.assertLess(c.get("strategies.min_score"), load_config("config/config.yaml").get("strategies.min_score"))
        self.assertLess(c.get("confluence.min_score"), load_config("config/config.yaml").get("confluence.min_score"))
        self.assertGreater(c.get("tracking.scalp_max_minutes"), 0)

    def test_scalp_min_rr2_reaches_risk_plan(self):
        from goldbot.engine.risk import RiskEngine
        from goldbot.config import load_config
        c = load_config("config/config.yaml", profile="scalp")
        self.assertTrue(RiskEngine(c).min_rr2 <= 1.4)     # العتبة انتقلت من الافتراضي إلى خطة المخاطرة


class TestNewStrategiesLoaded(unittest.TestCase):
    """الاستراتيجيتان المضافتان (ORB والأرقام المستديرة) مُسجَّلتان ولهما ترجمة عربية."""

    def test_opening_range_and_round_number_registered(self):
        from goldbot.strategies.impl import ALL
        from goldbot import i18n as T
        keys = [c.key for c in ALL]
        self.assertIn("opening_range", keys)
        self.assertIn("round_number", keys)
        self.assertIn("Opening Range", T.STRATEGY)
        self.assertIn("Round Number", T.STRATEGY)

    def test_opening_range_in_enabled_config(self):
        c = cfg()
        self.assertIn("opening_range", c.get("strategies.enabled"))


class TestScalpHtfRelaxation(unittest.TestCase):
    """السكالب يخفّف بوابة تعارض الفريمات: الوزن يبقى لكن لا يمنع الإعداد."""

    def _ctx(self):
        return SimpleNamespace(mtf={"bias": "conflict", "strength": "WEAK"}, spread=0.3,
                               tf={"15m": SimpleNamespace(last={"er": 0.5}, trend="RANGE"),
                                   "1H": SimpleNamespace(trend="RANGE")},
                               session={"label": "Asia"})

    def test_default_blocks_htf_conflict(self):
        from goldbot.engine.no_trade import global_gates
        from goldbot.config import load_config
        c = load_config("config/config.yaml")
        self.assertIn("HTF_CONFLICT(4H vs 1H)", global_gates(c, self._ctx(), True, "OK"))

    def test_scalp_ignores_htf_conflict(self):
        from goldbot.engine.no_trade import global_gates
        from goldbot.config import load_config
        c = load_config("config/config.yaml", profile="scalp")
        self.assertNotIn("HTF_CONFLICT(4H vs 1H)", global_gates(c, self._ctx(), True, "OK"))

    def test_scalp_drops_htf_requirement(self):
        from goldbot.strategies.base import htf_required
        from goldbot.config import load_config
        self.assertTrue(htf_required(load_config("config/config.yaml")))
        self.assertFalse(htf_required(load_config("config/config.yaml", profile="scalp")))


class TestWhyCommand(unittest.TestCase):
    """لوحة التشخيص: تشرح لماذا لا توجد توصية (البوابة التي أوقفتها)."""

    def test_why_reports_gate_when_no_signal(self):
        c, app = mkapp()
        cand = SimpleNamespace(ctx=None, best=None, results=[], confluence=0.0,
                               global_reasons=[], reasons=["CHOPPY_MARKET(ER=0.05)"],
                               log=SimpleNamespace(ts="2026-06-01 10:00 UTC", final="NO TRADE"))
        app.last_cand = cand
        out = build_handlers(app)["/why"]([])
        self.assertIn("لماذا لا توجد توصية", out)
        self.assertIn("سوق متذبذب", out)

    def test_why_reports_sent_signal(self):
        c, app = mkapp()
        b = SimpleNamespace(signal="BUY", name="Liquidity Sweep")
        cand = SimpleNamespace(ctx=object(), best=b, results=[b], confluence=75.0,
                               global_reasons=[], reasons=[],
                               log=SimpleNamespace(ts="x", final="BUY SENT"))
        app.last_cand = cand
        out = build_handlers(app)["/why"]([])
        self.assertIn("أُرسلت", out)


if __name__ == "__main__":
    unittest.main()

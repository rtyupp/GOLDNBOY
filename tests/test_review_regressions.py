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


if __name__ == "__main__":
    unittest.main()

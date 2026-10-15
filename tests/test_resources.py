import asyncio, os, sys, tempfile, time, unittest
from unittest import mock
import pandas as pd

from goldbot import resources as R
from goldbot import bootstrap as B
from goldbot import i18n as T
from tests.helpers import cfg, mkapp


def ts(s):
    return pd.Timestamp(s, tz="UTC")


class TestMarketClosed(unittest.TestCase):
    def test_weekend_window_edges(self):
        self.assertFalse(R.market_closed(ts("2026-10-09 20:59")))   # الجمعة قبل الإغلاق
        self.assertTrue(R.market_closed(ts("2026-10-09 21:00")))
        self.assertTrue(R.market_closed(ts("2026-10-10 12:00")))    # السبت
        self.assertTrue(R.market_closed(ts("2026-10-11 21:59")))    # الأحد قبل الافتتاح
        self.assertFalse(R.market_closed(ts("2026-10-11 22:00")))
        self.assertFalse(R.market_closed(ts("2026-10-13 10:00")))   # وسط الأسبوع


class TestPlan(unittest.TestCase):
    def _plan(self, lim, use=100.0, **over):
        c = cfg(bootstrap__adaptive=True, bootstrap__days=60, **over)
        with mock.patch.object(R, "limit_mb", return_value=lim), mock.patch.object(R, "usage_mb", return_value=use):
            return R.plan_bootstrap(c)

    def test_free_tier_512mb_uses_viable_window(self):
        p = self._plan(512.0, 100.0)
        self.assertTrue(p["run"])
        # 7 أيام/15m كانت تُنتج 0 صفقة (المحلل يحتاج 60 شمعة 4H ≈ 10 أيام)؛ الآن نافذة أصغر لكن صالحة
        self.assertEqual((p["days"], p["tfs"]), (45, ["5m", "15m"]))

    def test_tiny_memory_skips(self):
        self.assertFalse(self._plan(300.0)["run"])

    def test_no_free_memory_postpones(self):
        self.assertFalse(self._plan(512.0, 450.0)["run"])

    def test_big_memory_keeps_config(self):
        p = self._plan(4096.0, 300.0)
        self.assertEqual((p["days"], p["tfs"]), (60, ["5m", "15m"]))

    def test_unknown_limit_keeps_config(self):
        self.assertEqual(self._plan(None)["days"], 60)

    def test_env_override(self):
        with mock.patch.dict(os.environ, {"MEMORY_LIMIT_MB": "512"}):
            self.assertEqual(R.limit_mb(), 512.0)


class TestRunnerMemoryGuard(unittest.IsolatedAsyncioTestCase):
    async def test_kills_child_when_memory_high(self):
        log = os.path.join(tempfile.mkdtemp(), "b.log")
        async def fast_wait(fs, timeout=None):                       # يسرّع الاستطلاع في الاختبار
            return await real_wait(fs, timeout=0.05)
        real_wait = asyncio.wait
        with mock.patch.object(R, "pressure", return_value=0.95), mock.patch.object(asyncio, "wait", fast_wait):
            t0 = time.time()
            rc, tail = await B._default_runner([sys.executable, "-c", "import time; time.sleep(30)"], log, 60)
        self.assertEqual(rc, B.MEM_KILL)
        self.assertLess(time.time() - t0, 10)

    async def test_normal_child_completes(self):
        log = os.path.join(tempfile.mkdtemp(), "b.log")
        with mock.patch.object(R, "pressure", return_value=0.2):
            rc, _ = await B._default_runner([sys.executable, "-c", "print(1)"], log, 60)
        self.assertEqual(rc, 0)


class TestBootstrapAdapts(unittest.IsolatedAsyncioTestCase):
    async def _run(self, runner, lim=512.0, use=100.0):
        c, app = mkapp(bootstrap__adaptive=True)
        app.bootstrap.runner = runner
        with mock.patch.object(R, "limit_mb", return_value=lim), mock.patch.object(R, "usage_mb", return_value=use):
            ok = await app.bootstrap.run(force=True)
        msgs = []
        while not app.notify_q.empty():
            msgs.append(app.notify_q.get_nowait())
        return ok, msgs, app

    async def test_low_memory_uses_small_plan(self):
        calls = []
        async def runner(cmd, log, timeout):
            calls.append(cmd); return 0, ""
        ok, msgs, _ = await self._run(runner)
        self.assertEqual(len(calls), 2)                              # 5m و15m: 15m وحده لا يصل لعتبة الالتقاء
        self.assertIn("45", calls[0]); self.assertIn("5m", calls[0])

    async def test_memory_kill_stops_gracefully(self):
        async def runner(cmd, log, timeout):
            return B.MEM_KILL, "الذاكرة 90% من الحد"
        ok, msgs, app = await self._run(runner)
        self.assertFalse(ok)
        self.assertTrue(any("حمايةً للخدمة" in m for m in msgs))
        self.assertFalse(app.bootstrap.running)

    async def test_tiny_memory_postpones_without_running(self):
        calls = []
        async def runner(cmd, log, timeout):
            calls.append(cmd); return 0, ""
        ok, msgs, _ = await self._run(runner, lim=300.0)
        self.assertEqual(calls, [])
        self.assertTrue(any("تأجّلت" in m for m in msgs))


class TestCycleMarketClosed(unittest.IsolatedAsyncioTestCase):
    async def test_stale_data_on_weekend_is_market_closed(self):
        c, app = mkapp()
        sat = ts("2026-10-10 22:15")
        with mock.patch("goldbot.app.pd.Timestamp.now", return_value=sat):
            cand = await app.cycle()
        self.assertIn("MARKET_CLOSED", cand.reasons)
        self.assertIn("مغلق", T.reason_ar("MARKET_CLOSED"))

    async def test_stale_data_midweek_stays_stale(self):
        c, app = mkapp()
        tue = ts("2026-10-13 10:00")
        with mock.patch("goldbot.app.pd.Timestamp.now", return_value=tue):
            cand = await app.cycle()
        self.assertNotIn("MARKET_CLOSED", cand.reasons)


if __name__ == "__main__":
    unittest.main()


class TestStatusShowsEnv(unittest.TestCase):
    def test_status_has_market_and_memory(self):
        from goldbot.commands import build_handlers
        c, app = mkapp()
        out = build_handlers(app)["/status"]([])
        self.assertIn("السوق:", out)
        self.assertIn("الذاكرة:", out)

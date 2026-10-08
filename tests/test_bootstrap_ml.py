import asyncio, json, os, re, subprocess, sys, tempfile, unittest
import numpy as np, pandas as pd
from tests.helpers import cfg
from tests.test_assistant_news import mkapp
from goldbot.ml.model import MLGate


def fill_history(app, n, source="backtest"):
    rows = []
    for i in range(n):
        rows.append(dict(ts=f"2026-08-{1 + i % 28:02d}T10:00:00+00:00", tf="5m", setup="Liquidity Sweep", direction="BUY", session="New York",
                         htf_bias="bull", structure="BULLISH/BOS", atr_bucket="mid", rsi_bucket="50-65", liquidity="sweep", regime="BULLISH",
                         r=1.0, outcome="TP2", score=80, duration_min=30, features={"a": 1.0}))
    app.journal.add_history(rows, source)


class TestBootstrapLogic(unittest.IsolatedAsyncioTestCase):
    async def test_needed_conditions(self):
        c, app = mkapp()
        need, why = app.bootstrap.needed()
        self.assertTrue(need)                                          # سجل فارغ
        fill_history(app, 200)
        self.assertTrue(app.bootstrap.needed()[0])                     # لا يوجد marker
        os.makedirs(os.path.dirname(app.bootstrap.marker), exist_ok=True)
        json.dump({"ts": pd.Timestamp.now(tz="UTC").isoformat()}, open(app.bootstrap.marker, "w"))
        self.assertFalse(app.bootstrap.needed()[0])                    # حديث وكافٍ
        json.dump({"ts": (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=9)).isoformat()}, open(app.bootstrap.marker, "w"))
        self.assertTrue(app.bootstrap.needed()[0])                     # قديم > 7 أيام

    async def test_run_sequence_notifications_and_marker(self):
        c, app = mkapp()
        cmds = []

        async def runner(cmd, log_path, timeout):
            cmds.append(cmd)
            return 0, ""
        app.bootstrap.runner = runner
        ok = await app.bootstrap.run(True)
        self.assertTrue(ok)
        self.assertEqual([x[x.index("--tf") + 1] for x in cmds], ["5m", "15m"])
        for x in cmds:
            self.assertIn("--store", x)
            self.assertIn("--config", x)
        msgs = []
        while not app.notify_q.empty():
            msgs.append(app.notify_q.get_nowait())
        self.assertTrue(msgs[0].startswith("🧠"))
        self.assertTrue(any("اكتملت التهيئة" in m for m in msgs))
        self.assertTrue(os.path.exists(app.bootstrap.marker))
        self.assertFalse(app.bootstrap.running)

    async def test_failure_notifies_and_no_marker(self):
        c, app = mkapp()

        async def runner(cmd, log_path, timeout):
            return 1, "boom"
        app.bootstrap.runner = runner
        self.assertFalse(await app.bootstrap.run(True))
        msgs = []
        while not app.notify_q.empty():
            msgs.append(app.notify_q.get_nowait())
        self.assertTrue(any("فشل بناء السجل" in m for m in msgs))
        self.assertFalse(os.path.exists(app.bootstrap.marker))
        self.assertFalse(app.bootstrap.running)

    async def test_no_double_run(self):
        c, app = mkapp()
        gate = asyncio.Event()

        async def runner(cmd, log_path, timeout):
            await gate.wait()
            return 0, ""
        app.bootstrap.runner = runner
        t = asyncio.create_task(app.bootstrap.run(True))
        await asyncio.sleep(0.05)
        self.assertFalse(await app.bootstrap.run(True))
        gate.set()
        await t


class TestMLActivation(unittest.TestCase):
    def mk(self, informative: bool):
        rng = np.random.default_rng(3)
        n = 700
        a = rng.normal(size=n)
        p = 1 / (1 + np.exp(-2.5 * a)) if informative else np.full(n, 0.5)
        r = np.where(rng.random(n) < p, 1.5, -1.0)
        return pd.DataFrame({"ts": pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC").astype(str),
                             "features": [json.dumps({"a": float(x), "b": float(rng.normal())}) for x in a], "r": r})

    def gate(self):
        return MLGate(cfg(ml__enabled=True, ml__model_path=os.path.join(tempfile.mkdtemp(), "m.pkl")))

    def test_informative_features_activate_and_survive_reload(self):
        g = self.gate()
        rep = g.train(self.mk(True))
        self.assertTrue(rep["passed"], rep)
        self.assertTrue(g.active)
        self.assertIn("مفعّل", g.describe())
        g2 = MLGate(cfg(ml__enabled=True, ml__model_path=g.path))
        self.assertTrue(g2.active)                                     # يُحمَّل بعد إعادة التشغيل

    def test_noise_stays_off_with_arabic_explanation(self):
        g = self.gate()
        rep = g.train(self.mk(False))
        self.assertFalse(rep["passed"])
        self.assertIn("غير مفعّل", g.describe())

    def test_too_few_trades_explained(self):
        g = self.gate()
        g.train(self.mk(True).head(50))
        self.assertIn("عدد الصفقات غير كافٍ", g.describe())


class TestChartIsEnglish(unittest.TestCase):
    def test_no_arabic_in_chart_source(self):
        src = open("goldbot/notify/chart.py", encoding="utf-8").read()
        self.assertIsNone(re.search("[\u0600-\u06FF]", src))


@unittest.skipUnless(os.environ.get("SLOW") == "1", "اختبار بطيء: SLOW=1")
class TestRealSubprocessBootstrap(unittest.IsolatedAsyncioTestCase):
    async def test_end_to_end(self):
        d = tempfile.mkdtemp()
        conf = f"""
symbol: XAUUSD
providers: {{live: siftingio_ws, history: synthetic, synthetic: {{days: 30, end: now, seed: 5}}}}
paths: {{journal: {d}/j.sqlite, log_dir: {d}/logs}}
data: {{cache_dir: {d}/cache, history_days: 30}}
backtest: {{report_dir: {d}/reports, steps: {{5m: 1, 15m: 1}}}}
bootstrap: {{days: 30, tfs: [15m], marker: {d}/b.json, timeout_sec: 900}}
ml: {{enabled: true, model_path: {d}/m.pkl}}
confluence: {{min_score: 60}}
"""
        path = os.path.join(d, "c.yaml")
        open(path, "w").write(conf)
        from goldbot.config import load_config
        from goldbot.app import BotApp
        from goldbot.providers.synthetic import SyntheticHistory
        from tests.helpers import FakeTG
        c = load_config(path)
        app = BotApp(c, tg=FakeTG(), history_provider=SyntheticHistory(c.section("providers.synthetic")))
        ok = await app.bootstrap.run(True)
        self.assertTrue(ok)
        n = app.bootstrap.backtest_trades()
        print("\nصفقات السجل:", n, "|", app.ml.describe())
        self.assertGreater(n, 0)
        self.assertTrue(os.path.exists(os.path.join(d, "b.json")))


if __name__ == "__main__":
    unittest.main()

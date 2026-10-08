import asyncio, os, tempfile, time, unittest
import pandas as pd
from tests.helpers import cfg, FakeTG
from goldbot.app import BotApp
from goldbot.ai.gemini_layer import GeminiLayer
from goldbot.core.market_data import MarketData
from goldbot.engine.pipeline import Pipeline
from goldbot.models import Tick
from goldbot.providers.synthetic import make_synthetic_1m, SyntheticHistory
from goldbot.notify.chart import render_chart
from goldbot.tracking.journal import Journal
from tests.test_engine import T


import copy, functools


def find_tradable(seed=11, days=40):
    df, cand = _find_tradable(seed, days)
    return df, copy.deepcopy(cand)       # isolate tests from each other


@functools.lru_cache(maxsize=4)
def _find_tradable(seed=11, days=40):
    c = cfg()
    df = make_synthetic_1m(days=days, seed=seed)
    md = MarketData(); md.load_history(df)
    pl = Pipeline(c)
    for ts in df.index[-4000::5]:
        as_of = ts + pd.Timedelta(minutes=1)
        px = float(df.loc[ts, "close"])
        cand = pl.prepare(md.frames(as_of), px - .15, px + .15, as_of, use_probability=False)
        if cand.tradable:
            return df, cand
    return df, None


class TestRiskEngine(unittest.TestCase):
    def test_every_signal_has_logical_levels(self):
        df, cand = find_tradable()
        self.assertIsNotNone(cand, "no tradable candidate found in synthetic data")
        p = cand.best.plan
        if p.direction == "BUY":
            self.assertTrue(p.sl < p.entry < p.tp1 < p.tp2)
        else:
            self.assertTrue(p.sl > p.entry > p.tp1 > p.tp2)
        self.assertGreaterEqual(p.rr2, 1.8)
        self.assertLessEqual(p.risk, 3.0 * cand.ctx.atr + 1e-6)
        self.assertGreaterEqual(p.risk, 0.8 * cand.ctx.atr - 1e-6)


class TestE2E(unittest.IsolatedAsyncioTestCase):
    async def test_live_candle_does_not_make_history_ready(self):
        app = BotApp(cfg(paths__journal=os.path.join(tempfile.mkdtemp(), "j.sqlite"), paths__log_dir=tempfile.mkdtemp()),
                     tg=FakeTG(), history_provider=SyntheticHistory())
        now_ms = int(time.time() * 1000)
        app.ticks.set_connected(True)
        app.ticks.on_tick(Tick(now_ms, 2650.0, 2650.2, 2650.1, now_ms))
        app.md.history_rows = 0
        ok, why = app.data_status(pd.Timestamp.now(tz="UTC"))
        self.assertFalse(ok)
        self.assertTrue(why.startswith("INSUFFICIENT_HISTORY"))

    async def test_stale_data_gives_no_trade_with_reason(self):
        app = BotApp(cfg(paths__journal=os.path.join(tempfile.mkdtemp(), "j.sqlite"), paths__log_dir=tempfile.mkdtemp()), tg=FakeTG(), history_provider=SyntheticHistory())
        cand = await app.cycle()
        self.assertEqual(cand.log.final, "NO TRADE")
        self.assertIn("NO_TICK_YET", cand.log.reasons)
        self.assertEqual(app.journal.stats()["total"], 0)

    async def test_signal_flow_telegram_journal_tracking(self):
        df, cand = find_tradable()
        tg = FakeTG()
        c = cfg(paths__journal=os.path.join(tempfile.mkdtemp(), "j.sqlite"), paths__log_dir=tempfile.mkdtemp())
        app = BotApp(c, tg=tg, history_provider=SyntheticHistory())
        g = GeminiLayer(c)
        g.adapter = lambda prompt: '{"decision":"%s","reason":"card consistent"}' % cand.best.signal
        app.gemini = g
        ai = await g.decide("card", cand.best.signal)
        app.pipeline.finalize(cand, ai)
        self.assertEqual(cand.log.final, f"{cand.best.signal} SENT")
        await app.emit_signal(cand, ai)
        self.assertEqual(len(tg.channel), 1)
        self.assertEqual(len(tg.photos), 1)
        self.assertGreater(tg.photos[0][0], 20_000)
        row = app.journal.last_signals(1)[0]
        for k in ("timestamp", "symbol", "direction", "entry", "sl", "tp1", "tp2", "strategy", "session", "score", "ai_decision", "status"):
            self.assertIsNotNone(row[k], k)
        # drive price through TP1 then TP2 using ticks -> exactly 2 notifications
        p = cand.best.plan
        buy = p.direction == "BUY"
        ms = int(time.time() * 1000)
        for i, px in enumerate([p.tp1, p.tp1, p.tp2, p.tp2]):
            bid = px if buy else px - 0.3
            app.tracker.invalidate()
            app.tracker.on_tick(Tick(ms + i, bid, bid + 0.3, bid, ms + i))
        msgs = []
        while not app.notify_q.empty():
            msgs.append(app.notify_q.get_nowait())
        self.assertEqual([m.split("\n")[0] for m in msgs], ["✅ تحقق الهدف الأول (TP1) — الذهب", "✅ تحقق الهدف الثاني (TP2) — الذهب"])
        self.assertEqual(app.journal.get(row["id"])["result"], "TP2")

    async def test_telegram_failure_does_not_record_signal(self):
        df, cand = find_tradable()
        class BadTG(FakeTG):
            def send_signal(self, t):
                return False
        c = cfg(paths__journal=os.path.join(tempfile.mkdtemp(), "j.sqlite"), paths__log_dir=tempfile.mkdtemp())
        app = BotApp(c, tg=BadTG(), history_provider=SyntheticHistory())
        ai = type("A", (), {"decision": cand.best.signal, "reason": "ok"})()
        await app.emit_signal(cand, ai)
        self.assertEqual(app.journal.stats()["total"], 0)

    async def test_open_trade_and_cooldown_limits(self):
        df, cand = find_tradable()
        c = cfg(paths__journal=os.path.join(tempfile.mkdtemp(), "j.sqlite"), paths__log_dir=tempfile.mkdtemp())
        app = BotApp(c, tg=FakeTG(), history_provider=SyntheticHistory())
        ai = type("A", (), {"decision": cand.best.signal, "reason": "ok"})()
        await app.emit_signal(cand, ai)
        cand.reasons = []
        app._apply_limits(cand, cand.ctx.ts + pd.Timedelta(minutes=5))
        self.assertTrue(any(r.startswith("OPEN_TRADE_EXISTS") for r in cand.reasons))
        self.assertTrue(any(r.startswith("COOLDOWN") for r in cand.reasons))

    async def test_commands_render(self):
        df, cand = find_tradable()
        c = cfg(paths__journal=os.path.join(tempfile.mkdtemp(), "j.sqlite"), paths__log_dir=tempfile.mkdtemp())
        app = BotApp(c, tg=FakeTG(), history_provider=SyntheticHistory())
        app.last_cand = cand
        for cmd in ("/help", "/status", "/price", "/analysis", "/signal", "/stats", "/history", "/last", "/market", "/session", "/news"):
            out = app.cmd.handle_text(cmd)
            self.assertTrue(out and "حدث خطأ" not in out, f"{cmd}: {out}")
        self.assertIn("الاتجاه:", app.cmd.handle_text("/analysis"))

    def test_chart_png(self):
        df, cand = find_tradable()
        png = render_chart(cand.ctx, cand.best.plan)
        self.assertEqual(png[:4], b"\x89PNG")
        open("/tmp/chart_test.png", "wb").write(png)


if __name__ == "__main__":
    unittest.main()

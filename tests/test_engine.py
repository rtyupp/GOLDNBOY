import asyncio, json, os, tempfile, unittest
import numpy as np, pandas as pd
from tests.helpers import cfg, FakeTG
from goldbot.models import Tick, RiskPlan
from goldbot.tracking.journal import Journal
from goldbot.tracking.tracker import TradeTracker
from goldbot.engine.probability import ProbabilityEngine, wilson
from goldbot.ai.gemini_layer import parse_decision, GeminiLayer
from goldbot.engine import no_trade
from goldbot.notify.telegram import CommandBot, format_signal
from goldbot.engine.risk import RiskEngine


def J():
    return Journal(os.path.join(tempfile.mkdtemp(), "j.sqlite"))


def add(j, direction="BUY", entry=2650.0, sl=2645.0, rr1=1.2, rr2=2.4, ts="2026-06-01T10:00:00+00:00"):
    s = 1 if direction == "BUY" else -1
    risk = abs(entry - sl)
    return j.add_signal(timestamp=ts, symbol="XAUUSD", direction=direction, entry=entry, sl=sl,
                        tp1=entry + s * rr1 * risk, tp2=entry + s * rr2 * risk, strategy="Liquidity Sweep",
                        session="New York", score=80, historical_probability=None, prob_n=0, ai_decision=direction,
                        ai_reason="ok", risk_level="Low", reason="x", fingerprint={"setup": "Liquidity Sweep", "direction": direction,
                        "session": "New York", "htf_bias": "bull", "structure": "BULLISH/BOS", "atr_bucket": "mid", "rsi_bucket": "50-65",
                        "liquidity": "sweep", "regime": "BULLISH"}, rr1=rr1, rr2=rr2)


def T(ms, bid, ask=None):
    return Tick(ms, bid, ask if ask is not None else bid + 0.3, bid, ms)


class TestTracker(unittest.TestCase):
    def setUp(self):
        self.j = J()
        self.msgs = []
        self.tr = TradeTracker(self.j, self.msgs.append, cfg())
        self.t0 = int(pd.Timestamp("2026-06-01T10:05:00Z").timestamp() * 1000)

    def feed(self, *prices):
        for i, p in enumerate(prices):
            self.tr.invalidate()
            self.tr.on_tick(T(self.t0 + i * 1000, p))

    def test_buy_tp1_then_tp2_each_once(self):
        sid = add(self.j)
        self.feed(2651, 2656.1, 2656.2, 2656.0, 2662.1, 2662.5, 2663)
        self.assertEqual([m.split("\n")[0] for m in self.msgs], ["✅ تحقق الهدف الأول (TP1) — الذهب", "✅ تحقق الهدف الثاني (TP2) — الذهب"])
        self.assertIn("Break Even", self.msgs[0])
        r = self.j.get(sid)
        self.assertEqual((r["status"], r["result"]), ("CLOSED", "TP2"))
        self.assertAlmostEqual(r["r"], 0.5 * 1.2 + 0.5 * 2.4)
        self.assertEqual(r["sl_current"], 2650.0)

    def test_sl_hit_once(self):
        sid = add(self.j)
        self.feed(2649, 2644.9, 2644.0, 2640)
        self.assertEqual(len(self.msgs), 1)
        self.assertTrue(self.msgs[0].startswith("❌ ضرب وقف الخسارة"))
        self.assertEqual(self.j.get(sid)["r"], -1.0)

    def test_break_even_after_tp1(self):
        sid = add(self.j)
        self.feed(2656.1, 2651, 2649.9)
        self.assertEqual(self.j.get(sid)["result"], "TP1_BE")
        self.assertAlmostEqual(self.j.get(sid)["r"], 0.6)
        self.assertEqual(len(self.msgs), 2)

    def test_sell_uses_ask_and_gap_through_tp1_tp2(self):
        sid = add(self.j, "SELL", 2650.0, 2655.0)
        self.tr.invalidate()
        self.tr.on_tick(T(self.t0, 2643.0, 2643.3))               # ask 2643.3 <= tp2 2638? no -> tp1 (2644) only
        self.assertEqual(self.j.get(sid)["status"], "TP1")
        self.tr.invalidate()
        self.tr.on_tick(T(self.t0 + 1000, 2637.0, 2637.3))
        self.assertEqual(self.j.get(sid)["result"], "TP2")

    def test_closed_trade_feeds_history_and_stats(self):
        add(self.j)
        self.feed(2644)
        self.assertEqual(len(self.j.history_trades()), 1)
        s = self.j.stats()
        self.assertEqual((s["closed"], s["sl"]), (1, 1))

    def test_expiry(self):
        sid = add(self.j, ts="2026-05-30T10:00:00+00:00")
        self.tr.check_expiry(pd.Timestamp("2026-06-01T12:00Z"), 2652.0, 2652.3)
        self.assertEqual(self.j.get(sid)["result"], "EXPIRED")


class TestProbability(unittest.TestCase):
    fp = {"setup": "Liquidity Sweep", "direction": "BUY", "session": "New York", "htf_bias": "bull", "structure": "BULLISH/BOS",
          "atr_bucket": "mid", "rsi_bucket": "50-65", "liquidity": "sweep", "regime": "BULLISH"}

    def rows(self, n, wins):
        out = []
        for i in range(n):
            r = dict(self.fp)
            r.update(ts=f"2026-05-{1 + i % 28:02d}T10:00:00+00:00", tf="5m", r=1.5 if i < wins else -1.0, outcome="x")
            out.append(r)
        return out

    def test_insufficient_sample_shows_no_percentage(self):
        j = J(); j.add_history(self.rows(12, 8), "backtest")
        p = ProbabilityEngine(j, cfg()).estimate(self.fp)
        self.assertFalse(p.sufficient)
        self.assertIn("غير كافٍ", p.text())
        self.assertEqual(p.n, 12)

    def test_sufficient(self):
        j = J(); j.add_history(self.rows(127, 83), "backtest")
        p = ProbabilityEngine(j, cfg()).estimate(self.fp)
        self.assertTrue(p.sufficient)
        self.assertEqual((p.n, p.wins, p.losses), (127, 83, 44))
        self.assertAlmostEqual(p.win_rate, 83 / 127)
        self.assertLess(p.ci_low, p.win_rate)

    def test_never_uses_future_trades(self):
        j = J(); j.add_history(self.rows(60, 40), "backtest")
        p = ProbabilityEngine(j, cfg()).estimate(self.fp, as_of=pd.Timestamp("2026-04-01", tz="UTC"))
        self.assertEqual(p.n, 0)

    def test_dissimilar_setup_not_matched(self):
        j = J(); j.add_history(self.rows(60, 40), "backtest")
        fp = dict(self.fp, setup="Breakout")
        self.assertFalse(ProbabilityEngine(j, cfg()).estimate(fp).sufficient)

    def test_wilson(self):
        lo, hi = wilson(5, 5)
        self.assertLess(lo, 0.6)               # 5/5 must NOT look like certainty


class TestGemini(unittest.IsolatedAsyncioTestCase):
    def test_parse(self):
        self.assertEqual(parse_decision('{"decision":"BUY","reason":"ok"}').decision, "BUY")
        self.assertEqual(parse_decision('```json\n{"decision":"NO TRADE","reason":"x"}\n```').decision, "NO TRADE")
        d = parse_decision("I think maybe buy")
        self.assertEqual((d.decision, d.ok), ("NO TRADE", False))

    async def test_adapter_is_used_and_fail_closed(self):
        c = cfg(); g = GeminiLayer(c)
        g.adapter = lambda prompt: '{"decision":"SELL","reason":"fine"}'
        self.assertEqual((await g.decide("card", "SELL")).decision, "SELL")
        async def boom(prompt):
            raise RuntimeError("quota")
        g.adapter = boom
        d = await g.decide("card", "SELL")
        self.assertEqual(d.decision, "NO TRADE")
        g.on_error = "pass"
        self.assertEqual((await g.decide("card", "SELL")).decision, "SELL")

    async def test_disabled_passthrough(self):
        g = GeminiLayer(cfg(ai__enabled=False))
        self.assertEqual((await g.decide("c", "BUY")).decision, "BUY")


class TestGates(unittest.TestCase):
    def test_data_failures_are_no_trade(self):
        for why in ("STALE_DATA(30s>20s)", "WS_DISCONNECTED", "NO_TICK_YET"):
            self.assertEqual(no_trade.global_gates(cfg(), None, False, why), [why])

    def test_ai_rejection_blocks(self):
        class B: signal = "BUY"
        class A: decision, reason = "NO TRADE", "contradiction"
        r = no_trade.setup_gates(cfg(), B(), 90, None, A())
        self.assertTrue(r and r[0].startswith("AI_REJECTED"))

    def test_low_confluence_and_unavailable_probability(self):
        class B: signal = "BUY"
        class P: sufficient, n = False, 3
        r = no_trade.setup_gates(cfg(), B(), 55, P())
        self.assertTrue(any(x.startswith("LOW_CONFLUENCE") for x in r))
        self.assertTrue(any(x.startswith("PROBABILITY_UNAVAILABLE") for x in r))


class TestTelegram(unittest.TestCase):
    def test_commands_and_admin_gate(self):
        tg = FakeTG()
        bot = CommandBot(tg, {"/price": lambda a: "PRICE", "/boom": lambda a: 1 / 0})
        bot.process_update({"update_id": 1, "message": {"text": "/price@mybot", "from": {"id": 1}, "chat": {"id": 55}}})
        bot.process_update({"update_id": 2, "message": {"text": "/price", "from": {"id": 999}, "chat": {"id": 56}}})
        bot.process_update({"update_id": 3, "message": {"text": "/boom", "from": {"id": 1}, "chat": {"id": 55}}})
        bot.process_update({"update_id": 4, "message": {"text": "/nope", "from": {"id": 1}, "chat": {"id": 55}}})
        self.assertEqual(tg.private[0], (55, "PRICE"))
        self.assertEqual(len(tg.private), 3)                  # stranger ignored; error + unknown replied
        self.assertIn("حدث خطأ في الأمر /boom", tg.private[1][1])

    def test_signal_format(self):
        p = RiskPlan("BUY", 2650.1, 2645.0, 2656.2, 2662.3, 5.1, 6.1, 12.2, 1.2, 2.4)
        t = format_signal(p, "Liquidity Sweep", "New York", 78, "65.3% (n=127)", "sweep + BOS")
        for s in ("الذهب XAU/USD", "🟢 شراء", "2650.10", "2645.00", "2656.20", "2662.30", "1:2.4", "78/100", "65.3%", "اصطياد السيولة", "نيويورك", "وقف الخسارة"):
            self.assertIn(s, t)


if __name__ == "__main__":
    unittest.main()

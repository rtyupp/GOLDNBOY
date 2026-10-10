"""Wires everything together. Live price path is independent of history/analysis/AI/Telegram failures."""
from __future__ import annotations
import asyncio, json, logging, os, time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
import pandas as pd

from goldbot.bootstrap import Bootstrapper
from goldbot.commands import build_handlers
from goldbot.config import Cfg, env
from goldbot import i18n as T
from goldbot.core.candle_builder import CandleBuilder
from goldbot.core.history_store import load_cached
from goldbot.core.market_data import MarketData
from goldbot.core.tick_store import TickStore
from goldbot.engine.pipeline import Pipeline
from goldbot.ml.model import MLGate
from goldbot.monitor import CycleRecorder
from goldbot.notify.chart import render_chart
from goldbot.notify.telegram import Telegram, CommandBot, format_signal
from goldbot.tracking.journal import Journal
from goldbot.tracking.tracker import TradeTracker

log = logging.getLogger("app")


class BotApp:
    def __init__(self, cfg: Cfg, tg: Optional[Telegram] = None,
                 history_provider=None, live_factory=None, journal: Optional[Journal] = None):
        self.cfg = cfg
        self.started = time.time()
        self.journal = journal or Journal(cfg.get("paths.journal", "data/journal.sqlite"))
        self.ml = MLGate(cfg) if cfg.get("ml.enabled", False) else None
        self.ticks = TickStore(cfg.get("data.max_tick_age_sec", 20))
        self.md = MarketData()
        self.builder = CandleBuilder(self.md.add_closed)
        self.pipeline = Pipeline(cfg, self.journal, ml=self.ml)
        self.recorder = CycleRecorder(cfg.get("paths.log_dir", "logs"))
        self.tg = tg
        self.history_provider = history_provider
        self.live_factory = live_factory
        self.notify_q: asyncio.Queue = asyncio.Queue()
        self.tracker = TradeTracker(self.journal, self.notify_q.put_nowait, cfg)
        self.ticks.add_listener(self.builder.on_tick)
        self.ticks.add_listener(self.tracker.on_tick)
        self.last_cand = None
        self.cmd = CommandBot(tg, build_handlers(self)) if tg else None
        self.live = None
        self._tasks: list = []
        self._stop = False
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.bootstrap = Bootstrapper(self)
        self._bg: set = set()

    # ------------------------------------------------------------------ tasks
    async def load_history(self):
        retry = float(self.cfg.get("data.history_retry_sec", 300))
        while not self._stop:
            try:
                df = await asyncio.to_thread(load_cached, self.history_provider, self.cfg.get("symbol", "XAUUSD"), "1m",
                                             int(self.cfg.get("data.history_days", 45)), self.cfg.get("data.cache_dir", "data/cache"))
                min_rows = int(self.cfg.get("data.min_history_rows", 14400))
                if len(df) < min_rows:
                    raise RuntimeError(f"التاريخ غير كافٍ: {len(df)} شمعة 1m (المطلوب {min_rows})")
                self.md.load_history(df)
                self.md.history_error = None
                log.info("تم تحميل البيانات التاريخية: %s", self.md.summary())
                if self.cfg.get("bootstrap.enabled", True):       # بناء السجل وتدريب ML تلقائيًا في الخلفية
                    t = asyncio.create_task(self.bootstrap.maybe_run())
                    self._bg.add(t)
                    t.add_done_callback(self._bg.discard)
                return
            except Exception as e:           # history failure never touches the live feed
                self.md.history_error = f"{type(e).__name__}: {e}"
                log.error("فشل تحميل البيانات التاريخية: %s (إعادة المحاولة بعد %.0f ثانية)", self.md.history_error, retry)
                await asyncio.sleep(retry)

    async def sender(self):
        while True:
            text = await self.notify_q.get()
            if self.tg is None or self.cfg.get("mode") == "paper":
                log.info("[تجريبي] %s", text.replace("\n", " | "))
                continue
            await asyncio.to_thread(self.tg.send_signal, text)

    async def analysis_loop(self):
        off = float(self.cfg.get("analysis.cycle_offset_sec", 3))
        while not self._stop:
            now = time.time()
            await asyncio.sleep(60 - (now % 60) + off)
            try:
                await self.cycle()
            except Exception:
                log.exception("انهارت دورة التحليل (البوت يستمر بالعمل)")

    async def housekeeping(self):
        """تحديث انتهاء الصفقات فقط؛ لا يعتمد التشغيل على أي مصدر أخبار خارجي."""
        while not self._stop:
            tk = self.ticks.last
            if tk is not None and tk.has_quote:
                self.tracker.check_expiry(pd.Timestamp.now(tz="UTC"), tk.bid, tk.ask)
            await asyncio.sleep(60)

    def data_status(self, as_of: pd.Timestamp):
        ok, why = self.ticks.check_fresh()
        if not ok:
            return False, why
        tk = self.ticks.last
        if not tk.has_quote:
            return False, "NO_QUOTE(bid/ask missing)"
        min_rows = int(self.cfg.get("data.min_history_rows", 14400))
        if self.md.history_rows < min_rows:
            return False, f"INSUFFICIENT_HISTORY({self.md.history_rows}<{min_rows})"
        lc = self.md.last_base_close()
        if lc is None:
            return False, "NO_CANDLES" + (f"(history error: {self.md.history_error})" if self.md.history_error else "")
        gap = (as_of - lc).total_seconds() / 60.0
        if gap > float(self.cfg.get("data.max_candle_gap_min", 5)):
            return False, f"CANDLE_GAP({gap:.0f}min)"
        return True, "OK"

    async def cycle(self):
        now = pd.Timestamp.now(tz="UTC")
        self.builder.flush(int(now.timestamp() * 1000))
        as_of = now.floor("1min")
        ok, why = self.data_status(as_of)
        tk = self.ticks.last
        bid, ask = (tk.bid, tk.ask) if (tk and tk.has_quote) else (0.0, 0.0)
        frames = await asyncio.to_thread(self.md.frames, as_of) if ok else {}
        cand = await asyncio.to_thread(self.pipeline.prepare, frames, bid, ask, as_of, ok, why)
        self.last_cand = cand
        if cand.tradable:
            self._apply_limits(cand, as_of)
        if cand.tradable:
            self.pipeline.finalize(cand)
            if cand.log.final.endswith("SENT"):
                await self.emit_signal(cand)
        elif not cand.log.final.endswith("SENT"):
            cand.log.final = "NO TRADE"
            cand.log.reasons = cand.reasons
        self.recorder.record(cand.log)
        return cand

    def _apply_limits(self, cand, as_of):
        n_open = len(self.journal.open_trades())
        if n_open >= int(self.cfg.get("signals.max_open_trades", 1)):
            cand.reasons.append(f"OPEN_TRADE_EXISTS({n_open})")
        last = self.journal.last_signal_time()
        cd = float(self.cfg.get("signals.cooldown_min", 30))
        if last is not None and (as_of - last).total_seconds() / 60.0 < cd:
            cand.reasons.append(f"COOLDOWN({cd:.0f}min)")
        if cand.reasons:
            cand.log.final, cand.log.reasons = "NO TRADE", cand.reasons

    async def emit_signal(self, cand):
        b, ctx, plan = cand.best, cand.ctx, cand.best.plan
        prob_txt = cand.prob.text() if (cand.prob and cand.prob.sufficient) else "غير كافٍ (لا توجد عينة تاريخية كافية)"
        text = format_signal(plan, b.name, ctx.session["label"], cand.confluence, prob_txt, T.strategy_reason_ar(b.reason))
        sent = True
        if self.tg is not None and self.cfg.get("mode") != "paper":
            res = await asyncio.to_thread(self.tg.send_signal, text)
            sent = bool(res)
            if sent:
                try:
                    png = await asyncio.to_thread(render_chart, ctx, plan, self.cfg.get("telegram.chart_tf", "5m"), int(self.cfg.get("telegram.chart_bars", 100)))
                    await asyncio.to_thread(self.tg.send_signal_photo, png, f"{T.DIR[plan.direction]} — {T.STRATEGY.get(b.name, b.name)}")
                except Exception:
                    log.exception("فشل رسم الشارت (الإشارة أُرسلت فعلًا)")
        else:
            log.info("[تجريبي] إشارة\n%s", text)
        if not sent:
            cand.log.final, cand.log.reasons = "NO TRADE", ["TELEGRAM_SEND_FAILED"]
            log.error("لم تُسجَّل الإشارة لأن الإرسال إلى تيليجرام فشل")
            return
        sid = self.journal.add_signal(
            timestamp=ctx.ts.isoformat(), symbol=self.cfg.get("symbol", "XAUUSD"), direction=plan.direction, entry=plan.entry, sl=plan.sl,
            tp1=plan.tp1, tp2=plan.tp2, strategy=b.name, session=ctx.session["label"], score=cand.confluence,
            historical_probability=(cand.prob.win_rate if cand.prob and cand.prob.sufficient else None),
            prob_n=(cand.prob.n if cand.prob else 0), risk_level=cand.risk_level,
            reason=b.reason, fingerprint=cand.fp, rr1=plan.rr1, rr2=plan.rr2)
        self.tracker.invalidate()
        log.info("تم تسجيل الإشارة #%d", sid)

    # ------------------------------------------------------------------ health endpoint (Render/Fly/etc.)
    async def health_server(self):
        port = int(env("PORT", "0") or 0)
        if not port:
            return

        async def handle(reader, writer):
            try:
                await asyncio.wait_for(reader.readline(), 5)
                ok, why = self.ticks.check_fresh()
                body = json.dumps({"ok": True, "ws": self.ticks.connected, "data_fresh": ok, "data": why,
                                   "tick_age": self.ticks.age_sec(), "uptime_h": round((time.time() - self.started) / 3600, 2)}).encode()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
                await writer.drain()
            except Exception:
                pass
            finally:
                writer.close()
        srv = await asyncio.start_server(handle, "0.0.0.0", port)
        log.info("نقطة الفحص /health تعمل على المنفذ %d", port)
        async with srv:
            await srv.serve_forever()

    # ------------------------------------------------------------------ keep-alive (Render free web service)
    async def keepalive(self):
        """Render's free web service sleeps after ~15 min without inbound HTTP, which would kill the live feed.
        Pinging our own public URL every few minutes counts as inbound traffic. RENDER_EXTERNAL_URL is set by Render
        automatically (or set KEEPALIVE_URL yourself, e.g. when you also use UptimeRobot, which is even more reliable)."""
        base = env("KEEPALIVE_URL") or env("RENDER_EXTERNAL_URL")
        if not base:
            return
        import requests
        url = base.rstrip("/") + "/health"
        every = max(60.0, float(self.cfg.get("render.keepalive_min", 10)) * 60)
        log.info("Keep-alive مفعّل: %s كل %.0f دقيقة", url, every / 60)
        while not self._stop:
            await asyncio.sleep(every)
            try:
                await asyncio.to_thread(requests.get, url, timeout=20)
            except Exception as e:
                log.debug("فشل ping الـ keep-alive: %s", e)

    # ------------------------------------------------------------------ run
    async def _supervise(self, name: str, factory):
        """يعيد تشغيل المهمة تلقائيًا إن انهارت (انهيار تيليجرام مثلًا لا يوقف السعر اللحظي)."""
        delay = 3.0
        while not self._stop:
            t0 = time.time()
            try:
                await factory()
                return                                  # انتهت طبيعيًا
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("انهارت المهمة «%s» وسيُعاد تشغيلها بعد %.0f ثانية", name, delay)
                await asyncio.sleep(delay)
                delay = 3.0 if time.time() - t0 > 120 else min(delay * 2, 60.0)

    async def run(self):
        self.loop = asyncio.get_running_loop()
        self.loop.set_default_executor(ThreadPoolExecutor(max_workers=24, thread_name_prefix="goldbot"))   # الافتراضي صغير (≈5) ويختنق
        if self.live_factory is None:
            from goldbot.providers.factory import make_live_provider
            self.live = make_live_provider(self.cfg, self.ticks.on_tick, self.ticks.set_connected)
        else:
            self.live = self.live_factory(self.ticks.on_tick, self.ticks.set_connected)
        jobs = [("السعر اللحظي", self.live.run), ("البيانات التاريخية", self.load_history), ("التحليل", self.analysis_loop),
                ("الإرسال", self.sender), ("الصيانة", self.housekeeping), ("فحص الصحة", self.health_server),
                ("keep-alive", self.keepalive)]
        if self.cmd:
            jobs.append(("أوامر تيليجرام", self.cmd.run))
        if self.tg and self.cfg.get("mode") != "paper":
            self.notify_q.put_nowait("🤖 تم تشغيل بوت الذهب.\n«لا صفقة» نتيجة صحيحة؛ لن تصلك إشارة إلا إذا اتفقت كل الفلاتر.\n"
                                     "استخدم الأزرار السفلية أو /status لمعرفة الحالة.")
        self._tasks = [asyncio.create_task(self._supervise(n, f)) for n, f in jobs]
        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            pass
        finally:
            for t in self._tasks:
                t.cancel()

    def stop(self):
        self._stop = True
        if self.live:
            self.live.stop()
        if self.cmd:
            self.cmd.stop()

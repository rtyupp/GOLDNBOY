"""Wires everything together. Live price path is independent of history/analysis/Telegram failures.
لا يوجد ذكاء اصطناعي ولا مصدر أخبار: القرار من الاستراتيجيات والمؤشرات وإدارة المخاطر فقط."""
from __future__ import annotations
import gc
import asyncio, json, logging, time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
import pandas as pd

from goldbot.assist.deepseek import DeepSeek, signal_facts
from goldbot.bootstrap import Bootstrapper
from goldbot.commands import build_handlers
from goldbot import resources
from goldbot.config import Cfg, env
from goldbot import i18n as T
from goldbot.core.candle_builder import CandleBuilder
from goldbot.core.history_store import load_cached
from goldbot.core.market_data import MarketData
from goldbot.core.tick_store import TickStore
from goldbot.engine.pipeline import Pipeline
from goldbot.monitor import CycleRecorder
from goldbot.notify.chart import render_chart
from goldbot.notify.telegram import Telegram, CommandBot, format_signal
from goldbot.persist import JournalSync
from goldbot.tracking.journal import Journal
from goldbot.tracking.tracker import TradeTracker

log = logging.getLogger("app")


class BotApp:
    def __init__(self, cfg: Cfg, tg: Optional[Telegram] = None, history_provider=None, live_factory=None,
                 journal: Optional[Journal] = None, news=None, persist=None):
        self.cfg = cfg
        self.started = time.time()
        self.journal = journal or Journal(cfg.get("paths.journal", "data/journal.sqlite"))
        self.ticks = TickStore(cfg.get("data.max_tick_age_sec", 20))
        self.md = MarketData()
        self.builder = CandleBuilder(self.md.add_closed)
        self.pipeline = Pipeline(cfg, self.journal, news=news)
        self.recorder = CycleRecorder(cfg.get("paths.log_dir", "logs"))
        self.tg = tg
        self.news = news
        self.persist = persist or JournalSync(cfg, journal=self.journal)
        self._near_miss_last: dict = {}
        self.history_provider = history_provider
        self.live_factory = live_factory
        self.notify_q: asyncio.Queue = asyncio.Queue()
        self.tracker = TradeTracker(self.journal, self.notify_q.put_nowait, cfg)
        self.ticks.add_listener(self.builder.on_tick)
        self.ticks.add_listener(self.tracker.on_tick)
        self.last_cand = None
        self.assist = DeepSeek(cfg)
        self._handlers = build_handlers(self)
        self.cmd = CommandBot(tg, self._handlers) if tg else None
        if self.cmd:
            self.cmd.free_text = self._free_text
            self.cmd.chart_for_command = self._price_chart
        self._summary_sent: set = set()
        self.live = None
        self._tasks: list = []
        self._stop = False
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.bootstrap = Bootstrapper(self)
        self._bg: set = set()

    # ------------------------------------------------------------------ tasks
    async def load_history(self):
        base_retry = float(self.cfg.get("data.history_retry_sec", 120))
        fails = 0
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
                if self.cfg.get("bootstrap.enabled", True):       # بناء السجل التاريخي تلقائيًا في الخلفية
                    t = asyncio.create_task(self.bootstrap.maybe_run())
                    self._bg.add(t)
                    t.add_done_callback(self._bg.discard)
                return
            except Exception as e:           # history failure never touches the live feed
                self.md.history_error = f"{type(e).__name__}: {e}"
                retry = min(base_retry * (2 ** fails), 900.0)          # تأخير متزايد حتى 15 دقيقة: لا نضغط على مصدر يحجبنا
                fails += 1
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
        """يغلق الصفقات المنتهية المدة (انتهاء صلاحية الصفقة)."""
        while not self._stop:
            tk = self.ticks.last
            if tk is not None and tk.has_quote:
                self.tracker.check_expiry(pd.Timestamp.now(tz="UTC"), tk.bid, tk.ask)
            pr = resources.pressure()
            if pr is not None and pr >= float(self.cfg.get("resources.gc_at", 0.75)):
                gc.collect()                         # حارس الذاكرة: تنظيف قبل أن تقتل Render الخدمة
                log.warning("ضغط ذاكرة %.0f%% من الحد — تم التنظيف", pr * 100)
            await asyncio.sleep(60)

    async def news_loop(self):
        """يحدّث تقويم الأخبار الاقتصادية دوريًا (مجاني، بلا مفاتيح). الفشل لا يوقف البوت."""
        if self.news is None or not self.news.enabled:
            return
        while not self._stop:
            try:
                await asyncio.to_thread(self.news.refresh)
            except Exception:
                log.exception("فشل تحديث تقويم الأخبار")
            await asyncio.sleep(1800)

    async def persist_loop(self):
        """ينسخ السجل إلى Gist خاص دوريًا حتى لا يضيع مع كل إعادة نشر على خطة بلا قرص."""
        if not self.persist.ready:
            return
        while not self._stop:
            await asyncio.sleep(60)
            try:
                await asyncio.to_thread(self.persist.push)
            except Exception:
                log.exception("فشل رفع السجل إلى Gist")

    # ------------------------------------------------------------------ one analysis cycle
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
        closed = (not ok) and resources.market_closed(now) and why.startswith(("STALE_DATA", "NO_TICK_YET", "WS_DISCONNECTED", "CANDLE_GAP"))
        if closed:
            why = "MARKET_CLOSED"
        tk = self.ticks.last
        bid, ask = (tk.bid, tk.ask) if (tk and tk.has_quote) else (0.0, 0.0)
        frames = await asyncio.to_thread(self.md.frames, as_of) if ok else {}
        cand = await asyncio.to_thread(self.pipeline.prepare, frames, bid, ask, as_of, ok, why)
        self.last_cand = cand
        if cand.tradable:
            self._apply_limits(cand, as_of)
        if cand.tradable:
            cand.log.final = f"{cand.best.signal} SENT"
            await self.emit_signal(cand)
        elif not cand.log.final.endswith("SENT"):
            cand.log.final = "NO TRADE"
            cand.log.reasons = cand.reasons
            self._maybe_near_miss(cand, as_of)
        if closed:                                   # لا نملأ السجل كل دقيقة أثناء العطلة: مرة كل 30 دقيقة
            last = getattr(self, "_closed_log_ts", None)
            if last is None or (now - last).total_seconds() >= 1800:
                self._closed_log_ts = now
                self.recorder.record(cand.log)
        else:
            self.recorder.record(cand.log)
        return cand

    def _maybe_near_miss(self, cand, as_of):
        """إعداد قوي (درجة عالية) مُنع بسبب بوابة — نبلّغ عنه حتى لا يكون الصمت غامضًا. بتهدئة زمنية لكل إعداد/اتجاه."""
        if self.tg is None or self.cfg.get("mode") == "paper" or not self.cfg.get("near_miss.enabled", True):
            return
        if cand.ctx is None or not cand.results:
            return
        thr = float(self.cfg.get("near_miss.score", 80))
        cooldown = float(self.cfg.get("near_miss.cooldown_min", 60))
        best = max(cand.results, key=lambda r: r.score)
        if best.score < thr:
            return
        key = (best.name, best.setup_tags.get("direction"))
        last = self._near_miss_last.get(key)
        if last is not None and (as_of - last).total_seconds() / 60.0 < cooldown:
            return
        self._near_miss_last[key] = as_of
        b = best
        gate = T.reasons_ar(cand.reasons) if cand.reasons else T.strategy_reason_ar(b.reason)
        self.notify_q.put_nowait(
            f"👀 فرصة قاربت — لم تتحقق شروط الدخول بعد\n"
            f"الإعداد: {T.STRATEGY.get(b.name, b.name)}   الاتجاه: {T.DIR.get(b.signal, 'لا إشارة')}   الدرجة: {b.score:.0f}\n"
            f"منعه: {gate}\n"
            f"(ليست توصية — تنبيه فقط لاقتراب إعداد قوي)")

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
        if self.assist.enabled:                         # الشرح يصل بعد الإشارة ولا يؤخّرها ولا يغيّرها
            t = asyncio.create_task(self._explain(cand, sid, prob_txt))
            self._bg.add(t)
            t.add_done_callback(self._bg.discard)

    async def _explain(self, cand, sid: int, prob_txt: str):
        try:
            b, ctx, plan = cand.best, cand.ctx, cand.best.plan
            facts = signal_facts(cand, plan, ctx, prob_txt, T.strategy_reason_ar(b.reason), T.STRATEGY.get(b.name, b.name),
                                 T.SESSION.get(ctx.session["label"], ctx.session["label"]), T.DIR[plan.direction])
            text = await asyncio.to_thread(self.assist.explain_signal, facts)
            if text:
                self.notify_q.put_nowait(f"📝 شرح الإشارة #{sid}\n{text}")
        except Exception:
            log.exception("فشل شرح الإشارة (الإشارة نفسها أُرسلت)")

    def _price_chart(self):
        """يرسم شارت السعر عند الضغط على زر السعر دون تعطيل الرد النصي."""
        c = self.last_cand
        if c is None or c.ctx is None:
            return None
        plan = c.best.plan if c.best is not None and getattr(c.best, "plan", None) is not None else None
        return render_chart(c.ctx, plan, self.cfg.get("telegram.chart_tf", "5m"),
                            int(self.cfg.get("telegram.chart_bars", 100)))

    # ------------------------------------------------------------------ الأسئلة الحرة (بدون زر: أي نص عادي)
    def _free_text(self, text: str, chat=None):
        if not self.assist.enabled:
            return None
        parts = []
        for cmd, title in (("/price", "السعر"), ("/signal", "آخر إعداد"), ("/stats", "الإحصائيات"), ("/session", "الجلسة")):
            try:
                parts.append(f"[{title}]\n{self._handlers[cmd]([])}")
            except Exception:
                log.warning("تعذّر تجهيز «%s» لسياق المساعد", title, exc_info=True)
        closed = resources.market_closed(pd.Timestamp.now(tz="UTC"))
        parts.append(f"[حالة السوق]\n{'مغلق (عطلة نهاية الأسبوع)' if closed else 'مفتوح'}")
        return self.assist.ask(text, "\n\n".join(parts), chat)

    # ------------------------------------------------------------------ الملخص اليومي/الأسبوعي (تلقائي)
    def build_summary(self, now: pd.Timestamp):
        """يرجع (نص الأرقام، أرقام للتعليق) أو None إن لم تكن هناك إشارات في الفترة. الأرقام من السجل فقط."""
        weekly = now.weekday() == 4
        since = now - pd.Timedelta(days=7 if weekly else 1)
        rows = [r for r in self.journal.last_signals(300) if pd.Timestamp(r["timestamp"]) >= since]
        if not rows:
            return None
        closed = [r for r in rows if r["r"] is not None]
        wins = sum(1 for r in closed if r["r"] > 0)
        total_r = sum(r["r"] for r in closed)
        by = {}
        for r in closed:
            by.setdefault(T.STRATEGY.get(r["strategy"], r["strategy"]), []).append(r["r"])
        lines = [f"{'الأسبوع' if weekly else 'اليوم'}: {len(rows)} إشارة، مغلقة {len(closed)}، مفتوحة {len(rows) - len(closed)}"]
        if closed:
            lines.append(f"رابحة {wins} / خاسرة {len(closed) - wins}   إجمالي R: {total_r:+.2f}")
            lines += [f"- {k}: {len(v)} صفقة، مجموع R {sum(v):+.2f}" for k, v in by.items()]
        facts = "\n".join(lines)
        return ("📊 ملخص الأسبوع" if weekly else "📊 ملخص اليوم") + "\n" + facts, facts

    async def summaries(self):
        hour = int(self.cfg.get("assist.summary_hour_utc", 20))
        while not self._stop:
            await asyncio.sleep(300)
            try:
                now = pd.Timestamp.now(tz="UTC")
                key = now.strftime("%Y-%m-%d")
                if now.weekday() > 4 or now.hour != hour or key in self._summary_sent:
                    continue
                self._summary_sent.add(key)
                res = await asyncio.to_thread(self.build_summary, now)
                if not res:
                    continue
                text, facts = res
                comment = await asyncio.to_thread(self.assist.comment_summary, facts) if self.assist.enabled else None
                self.notify_q.put_nowait(text + (f"\n\n{comment}" if comment else ""))      # الأرقام تصل دائمًا؛ التعليق إن توفر
            except Exception:
                log.exception("فشل الملخص الدوري")

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
        # استعادة السجل من Gist قبل أي شيء حتى يُستخدم في الاحتمال التاريخي فورًا
        if self.persist.ready:
            await asyncio.to_thread(self.persist.restore)
        if self.live_factory is None:
            from goldbot.providers.factory import make_live_provider
            self.live = make_live_provider(self.cfg, self.ticks.on_tick, self.ticks.set_connected)
        else:
            self.live = self.live_factory(self.ticks.on_tick, self.ticks.set_connected)
        jobs = [("السعر اللحظي", self.live.run), ("البيانات التاريخية", self.load_history), ("التحليل", self.analysis_loop),
                ("الإرسال", self.sender), ("الصيانة", self.housekeeping), ("الملخصات", self.summaries), ("فحص الصحة", self.health_server),
                ("الأخبار", self.news_loop), ("حفظ السجل", self.persist_loop), ("keep-alive", self.keepalive)]
        if self.cmd:
            jobs.append(("أوامر تيليجرام", self.cmd.run))
        if self.tg and self.cfg.get("mode") != "paper":
            self.notify_q.put_nowait("🤖 تم تشغيل بوت GOLDNBOY.\nأحلل السوق كل دقيقة، وسترسل لك الإشارة فور اكتمال شروطها.\n"
                                     "استخدم الأزرار بالأسفل أو اكتب /start.")
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
        try:                                    # آخر نسخة من السجل قبل الإيقاف (نشر/إعادة تشغيل)
            self.persist.push(force=True)
        except Exception:
            log.debug("تعذّر حفظ السجل عند الإيقاف", exc_info=True)

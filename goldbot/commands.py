"""أوامر تيليجرام بالعربي (أسماء الأوامر لاتينية لأن تيليجرام يشترط ذلك)."""
from __future__ import annotations
from goldbot import resources
import time
import pandas as pd
from goldbot import i18n as T


def _age(a):
    return "غير متاح" if a is None else f"{a:.1f} ثانية"


def _mem_txt() -> str:
    lim, use = resources.limit_mb(), resources.usage_mb()
    if use is None:
        return "غير معروفة"
    return f"{use:.0f}/{lim:.0f}MB" if lim else f"{use:.0f}MB"


def build_handlers(app) -> dict:
    def help_(args):
        return ("استخدم الأزرار بالأسفل، أو الأوامر:\n/start  القائمة والترحيب\n/price  السعر اللحظي\n/analysis  التحليل الحالي\n"
                "/signal  آخر إعداد تم تقييمه\n/market  نظرة متعددة الفريمات\n/stats  الإحصائيات\n/history  آخر الإشارات\n"
                "/last  آخر إشارة\n/session  الجلسة الحالية\n/status  حالة النظام\n/train  إعادة بناء السجل التاريخي\n/id  رقم حسابك")

    def status(args):
        t = app.ticks
        fresh, why = t.check_fresh()
        lc = app.recorder.last
        op = app.journal.open_trades()
        min_rows = int(app.cfg.get("data.min_history_rows", 14400))
        if app.md.history_rows >= min_rows:
            hist = f"سليمة ({app.md.history_rows} شمعة)"
        elif app.md.history_error:
            hist = f"خطأ: {app.md.history_error}"
        else:
            hist = f"قيد التحميل ({app.md.history_rows}/{min_rows})"
            prog = getattr(app.history_provider, "progress", None)
            if prog and prog[1]:
                hist += f" — تنزيل Dukascopy: {prog[0]}/{prog[1]} ساعة"
        return "\n".join([
            "حالة النظام",
            f"الاتصال اللحظي: {'متصل ✅' if t.connected else 'منقطع ❌'}   عدد التحديثات: {t.count}",
            f"السعر اللحظي: {'حديث ✅' if fresh else 'غير صالح ❌ (' + T.reason_ar(why) + ')'}   عمر آخر تحديث: {_age(t.age_sec())}",
            f"الشموع: {app.md.summary()}",
            f"البيانات التاريخية: {hist}",
            f"السوق: {'مغلق (عطلة نهاية الأسبوع)' if resources.market_closed(pd.Timestamp.now(tz='UTC')) else 'مفتوح'}   الذاكرة: {_mem_txt()}",
            f"الوضع: {'حقيقي' if app.cfg.get('mode') == 'live' else 'تجريبي (بدون إرسال)'}   الاستراتيجيات: {len(app.pipeline.strategies)}",
            f"التهيئة الذاتية: {'قيد التنفيذ ⏳' if app.bootstrap.running else app.bootstrap.last_summary}   سجل الاحتمال: {app.bootstrap.backtest_trades()} صفقة",
            f"وجهة الإشارات: {({'private': 'المحادثة الخاصة', 'channel': 'القناة', 'both': 'الخاص والقناة'}).get(app.tg.target if app.tg else '', 'لا يوجد')}",
            f"صفقات مفتوحة: {len(op)}   مدة التشغيل: {(time.time() - app.started) / 3600:.1f} ساعة",
            f"آخر دورة تحليل: {lc.ts if lc else '-'} ← {T.final_ar(lc.final) if lc else '-'}" + (f"\nالسبب: {T.reasons_ar(lc.reasons)[:200]}" if lc and lc.reasons else ""),
        ])

    def price(args):
        tk = app.ticks.last
        if tk is None:
            return "لم يصل أي سعر بعد."
        return (f"الذهب XAUUSD\nBid {tk.bid}   Ask {tk.ask}   السبريد {tk.spread if tk.spread is not None else 'غير متاح'}\n"
                f"عمر التحديث: {_age(app.ticks.age_sec())}   {'حديث ✅' if app.ticks.check_fresh()[0] else 'قديم/غير صالح ❌'}")

    def analysis(args):
        c = app.last_cand
        if c is None or c.ctx is None:
            return "لا يوجد تحليل بعد." + (f"\nالسبب: {T.reasons_ar(c.reasons)}" if c else "")
        ctx, b = c.ctx, c.best
        l = ctx.tf["5m"].last
        s5 = ctx.tf["5m"]
        lb = s5.last_break
        sw = next((s for s in s5.sweeps + ctx.tf["15m"].sweeps), None)
        return "\n".join([
            f"الاتجاه:\n{T.TREND[ctx.mtf['labels']['1H']]} (4 ساعات: {T.TREND[ctx.mtf['labels']['4H']]})", "",
            f"الهيكل:\n{T.BREAK[lb.kind] + ' ' + T.DIR[lb.direction] if lb else 'لا يوجد'}", "",
            f"السيولة:\n{(T.SWEEP_KIND.get(sw.kind, sw.kind) + ' ' + ('للأعلى' if sw.expected == 'bull' else 'للأسفل')) if sw else 'لا يوجد'}", "",
            f"VWAP:\n{'السعر فوقه' if l['close'] > l['vwap'] else 'السعر تحته'}", "",
            f"RSI:\n{l['rsi']:.0f}", "", f"ATR:\n{s5.atr:.2f}", "", f"الجلسة:\n{T.SESSION.get(ctx.session['label'], ctx.session['label'])}", "",
            f"الإشارة:\n{T.DIR[b.signal] if b and b.signal != 'NONE' else 'لا يوجد'}" + (f" ({T.STRATEGY.get(b.name, b.name)})" if b and b.signal != 'NONE' else ""), "",
            f"الدرجة:\n{c.confluence:.0f}", "",
            f"الاحتمال التاريخي:\n{c.prob.text() if c.prob else 'غير متاح'}", "",
            f"القرار:\n{T.final_ar(c.log.final)}" + (f"\nالسبب: {T.reasons_ar(c.reasons)[:250]}" if c.reasons else "")])

    def signal(args):
        c = app.last_cand
        if c is None:
            return "لم تتم أي دورة تحليل بعد."
        if c.best is None or c.best.signal == "NONE":
            lines = [f"- {T.STRATEGY.get(r.name, r.name)}: {T.strategy_reason_ar(r.reason)[:90]}" for r in c.results[:5]]
            return "لا يوجد إعداد الآن.\n" + "\n".join(lines) + (f"\nالموانع: {T.reasons_ar(c.reasons)}" if c.reasons else "")
        p = c.best.plan
        return (f"إعداد مرشّح: {T.DIR[p.direction]} ({T.STRATEGY.get(c.best.name, c.best.name)})\nالدخول {p.entry}   وقف الخسارة {p.sl}\n"
                f"الهدف الأول {p.tp1}   الهدف الثاني {p.tp2}\nالعائد/المخاطرة 1:{p.rr2}   الدرجة {c.confluence:.0f}\n"
                f"القرار: {T.final_ar(c.log.final)}" + (f"\nمنعته: {T.reasons_ar(c.reasons)}" if c.reasons else ""))

    def stats(args):
        s = app.journal.stats()
        if not s.get("closed"):
            return f"عدد الإشارات: {s['total']}   مفتوحة: {s['open']}   مغلقة: 0 (لا توجد نتائج بعد)"
        pf = "∞" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
        out = [f"الإشارات {s['total']}   مفتوحة {s['open']}   مغلقة {s['closed']}",
               f"نسبة الربح {s['win_rate']:.1%}   رابحة/خاسرة {s['wins']}/{s['losses']}",
               f"متوسط R {s['avg_r']:.2f}   معامل الربح {pf}   إجمالي R {s['total_r']:.2f}",
               f"الهدف الأول {s['tp1']}   الهدف الثاني {s['tp2']}   وقف الخسارة {s['sl']}", "", "حسب الاستراتيجية:"]
        out += [f"- {T.STRATEGY.get(k, k)}: عدد {v['count']}  متوسط R {v['mean']}" for k, v in s.get("by_strategy", {}).items()]
        out += ["", "حسب الجلسة:"] + [f"- {T.SESSION.get(k, k)}: عدد {v['count']}  متوسط R {v['mean']}" for k, v in s.get("by_session", {}).items()]
        return "\n".join(out)

    def history(args):
        rows = app.journal.last_signals(10)
        if not rows:
            return "لا توجد إشارات بعد."
        return "\n".join(f"#{r['id']} {r['timestamp'][5:16]} {T.DIR[r['direction']]} {r['entry']:.2f} {T.STRATEGY.get(r['strategy'], r['strategy'])} ← {T.RESULT.get(r['result'] or r['status'], r['result'] or r['status'])}"
                         + (f" ({r['r']:+.2f}R)" if r['r'] is not None else "") for r in rows)

    def last(args):
        rows = app.journal.last_signals(1)
        if not rows:
            return "لا توجد إشارات بعد."
        r = rows[0]
        prob = f"{r['historical_probability']:.1%} (عينة {r['prob_n']})" if r['historical_probability'] is not None else "غير متاح"
        return (f"#{r['id']} {T.DIR[r['direction']]} — {T.STRATEGY.get(r['strategy'], r['strategy'])} ({T.SESSION.get(r['session'], r['session'])})\n{r['timestamp']}\n"
                f"الدخول {r['entry']:.2f}  وقف الخسارة {r['sl']:.2f}  الهدف1 {r['tp1']:.2f}  الهدف2 {r['tp2']:.2f}\n"
                f"الدرجة {r['score']:.0f}   الاحتمال التاريخي {prob}\n"
                f"الحالة: {T.RESULT.get(r['result'] or r['status'], r['result'] or r['status'])}" + ("" if r['r'] is None else f"  {r['r']:+.2f}R"))

    def market(args):
        c = app.last_cand
        if c is None or c.ctx is None:
            return "لا يوجد تحليل بعد."
        ctx = c.ctx
        lines = [f"السوق  {ctx.mid:.2f}", f"الاتجاه العام: {T.TREND.get(ctx.mtf['bias'], ctx.mtf['bias'])} ({T.STRENGTH[ctx.mtf['strength']]})"]
        for tf in ("4H", "1H", "15m", "5m", "1m"):
            s = ctx.tf[tf]
            lb = s.last_break
            lines.append(f"{tf}: {T.TREND[s.trend]}  {(T.BREAK[lb.kind] + ' ' + T.DIR[lb.direction]) if lb else ''}  ATR {s.atr:.2f}")
        lines.append("المستويات: " + "  ".join(f"{k} {v:.2f}" for k, v in list(ctx.levels.items())[:8]))
        return "\n".join(lines)

    def session(args):
        c = app.last_cand
        if c is None or c.ctx is None:
            return "لا يوجد تحليل بعد."
        s = c.ctx.session
        orr = s["opening_range"]
        bo = {"LONDON_BREAKOUT_UP": "اختراق لندن للأعلى", "LONDON_BREAKOUT_DOWN": "اختراق لندن للأسفل",
              "NY_BREAKOUT_UP": "اختراق نيويورك للأعلى", "NY_BREAKOUT_DOWN": "اختراق نيويورك للأسفل"}.get(s["breakout"], "لا يوجد")
        return (f"الجلسة: {T.SESSION.get(s['label'], s['label'])}\nالأعلى {s['high']}   الأدنى {s['low']}   المدى {None if s['range'] is None else round(s['range'], 2)}\n"
                f"النطاق الافتتاحي: {orr}\nالاختراق: {bo}")

    def train(args):
        if app.bootstrap.running:
            return "التهيئة تعمل الآن، سأخبرك عند الانتهاء."
        if app.loop is None:
            return "النظام لم يكتمل تشغيله بعد."
        import asyncio as _a
        _a.run_coroutine_threadsafe(app.bootstrap.run(True), app.loop)
        return "🧠 بدأت إعادة بناء السجل التاريخي في الخلفية (5–20 دقيقة)، وسأرسل لك النتيجة."

    return {"/train": train, "/help": help_, "/status": status, "/price": price, "/analysis": analysis, "/signal": signal,
            "/stats": stats, "/history": history, "/last": last, "/market": market, "/session": session}

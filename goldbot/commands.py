"""أوامر تيليجرام بالعربي (أسماء الأوامر لاتينية لأن تيليجرام يشترط ذلك)."""
from __future__ import annotations
import time
import pandas as pd
from goldbot import i18n as T


def _age(a):
    return "غير متاح" if a is None else f"{a:.1f} ثانية"


def build_handlers(app) -> dict:
    def help_(args):
        return ("الأوامر المتاحة:\n/status  حالة النظام\n/price  السعر اللحظي\n/analysis  التحليل الحالي\n/signal  آخر إعداد تم تقييمه\n"
                "/stats  الإحصائيات\n/history  آخر الإشارات\n/last  آخر إشارة\n/market  نظرة متعددة الفريمات\n"
                "/session  الجلسة الحالية\n/news  الأخبار الحية وتقويم الأحداث\n/chart  الشارت مع التفاصيل\n"
                "/ask سؤالك  محادثة تفهم السوق والكود\n/id  رقم حسابك\n\nيمكنك أيضًا إرسال رسالة عادية للمساعد.")

    def status(args):
        t = app.ticks
        fresh, why = t.check_fresh()
        lc = app.recorder.last
        op = app.journal.open_trades()
        ml = app.ml
        n_ev = len(app.news.events)
        return "\n".join([
            "حالة النظام",
            f"الاتصال اللحظي: {'متصل ✅' if t.connected else 'منقطع ❌'}   عدد التحديثات: {t.count}",
            f"السعر اللحظي: {'حديث ✅' if fresh else 'غير صالح ❌ (' + T.reason_ar(why) + ')'}   عمر آخر تحديث: {_age(t.age_sec())}",
            f"الشموع: {app.md.summary()}",
            f"البيانات التاريخية: {'سليمة (' + str(app.md.history_rows) + ' شمعة)' if not app.md.history_error else 'خطأ: ' + app.md.history_error}",
            f"تقويم الأخبار: {n_ev} حدث" + ("  ⚠️ فارغ — فلتر الأخبار غير فعّال" if n_ev == 0 else ""),
            f"الوضع: {'حقيقي' if app.cfg.get('mode') == 'live' else 'تجريبي (بدون إرسال)'}   الذكاء الاصطناعي: {'مفعّل (' + app.gemini.model + ')' if app.gemini.enabled else 'معطّل'}   التعلم الآلي: {'فعّال' if (ml and ml.active) else 'غير مفعّل'}",
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
                f"الدرجة {r['score']:.0f}   الاحتمال التاريخي {prob}\nقرار الذكاء الاصطناعي: {T.DIR.get(r['ai_decision'], r['ai_decision'])} — {r['ai_reason']}\n"
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

    def news(args):
        now = pd.Timestamp.now(tz="UTC")
        st = app.news.status(now)
        nxt = app.news.next_events(now, 5)
        lines = [f"حالة الأخبار: {T.NEWS_STATE[st.state]}" + (f" ({st.event})" if st.event else "")]
        lines += [f"{d:%Y-%m-%d %H:%M} UTC  {n}" for d, n in nxt] or ["لا توجد أحداث قادمة في ملف data/events.csv (أضفها!)"]
        if getattr(app, "news_feed", None):
            lines += ["", app.news_feed.format_ar(6)]
        return "\n".join(lines)

    def chart(args):
        return app.chart_payload(args[0] if args and args[0] in ("1m", "5m", "15m", "1H", "4H") else None)

    def ask(args):
        return app.ask(" ".join(args).strip()) if args else "اكتب سؤالك بعد /ask، مثال: /ask اشرح لي سبب عدم وجود صفقة"

    return {"/help": help_, "/start": help_, "/status": status, "/price": price, "/analysis": analysis, "/signal": signal,
            "/stats": stats, "/history": history, "/last": last, "/market": market, "/session": session, "/news": news,
            "/chart": chart, "/ask": ask}

"""أوامر تيليجرام العربية المعتمدة على التحليل المحلي فقط."""
from __future__ import annotations
import time
from goldbot import i18n as T

def _age(a): return "غير متاح" if a is None else f"{a:.1f} ثانية"

def build_handlers(app) -> dict:
    def help_(args):
        return ("ارحبو تراحيب المطر\n\nبوت GOLDNBOY لإشارات تداول الذهب XAUUSD، يعتمد على الأسعار اللحظية والاستراتيجيات والمؤشرات الفنية لتحليل السوق وتحديد فرص التداول المحتملة.\n\nby: @QUOP9\n\n"
                "الأوامر المتاحة:\n/status السعر اللحظي /price\n/analysis التحليل الحالي\n/signal آخر إعداد\n/stats الإحصائيات\n/history آخر الإشارات\n/last آخر إشارة\n/market نظرة السوق\n/session الجلسة الحالية\n/train إعادة بناء السجل\n/id رقم حسابك")
    def status(args):
        t, lc, op, ml = app.ticks, app.recorder.last, app.journal.open_trades(), app.ml
        fresh, why = t.check_fresh(); minimum = int(app.cfg.get("data.min_history_rows", 14400))
        hist = f"سليمة ({app.md.history_rows} شمعة)" if app.md.history_rows >= minimum else (f"خطأ: {app.md.history_error}" if app.md.history_error else f"قيد التحميل ({app.md.history_rows}/{minimum})")
        return "\n".join(["حالة النظام", f"الاتصال اللحظي: {'متصل ✅' if t.connected else 'منقطع ❌'}   عدد التحديثات: {t.count}", f"السعر اللحظي: {'حديث ✅' if fresh else 'غير صالح ❌ (' + T.reason_ar(why) + ')'}   عمر آخر تحديث: {_age(t.age_sec())}", f"الشموع: {app.md.summary()}", f"البيانات التاريخية: {hist}", f"الوضع: {'حقيقي' if app.cfg.get('mode') == 'live' else 'تجريبي (بدون إرسال)'}   القرار: محلي بالكامل", (ml.describe() if ml else "التعلم الآلي: معطّل في الإعدادات"), f"التهيئة الذاتية: {'قيد التنفيذ ⏳' if app.bootstrap.running else app.bootstrap.last_summary}   سجل الاحتمال: {app.bootstrap.backtest_trades()} صفقة", f"صفقات مفتوحة: {len(op)}   مدة التشغيل: {(time.time() - app.started) / 3600:.1f} ساعة", f"آخر دورة تحليل: {lc.ts if lc else '-'} ← {T.final_ar(lc.final) if lc else '-'}"])
    def price(args):
        tk=app.ticks.last
        if tk is None: return "لم يصل أي سعر بعد."
        return f"الذهب XAUUSD\nBid {tk.bid}   Ask {tk.ask}   السبريد {tk.spread if tk.spread is not None else 'غير متاح'}\nعمر التحديث: {_age(app.ticks.age_sec())}"
    def analysis(args):
        c=app.last_cand
        if c is None or c.ctx is None: return "لا يوجد تحليل بعد."
        ctx,b,l,s5=c.ctx,c.best,c.ctx.tf["5m"].last,c.ctx.tf["5m"]; lb=s5.last_break
        return f"الاتجاه: {T.TREND[ctx.mtf['labels']['1H']]} / 4H {T.TREND[ctx.mtf['labels']['4H']]}\nVWAP: {'فوقه' if l['close'] > l['vwap'] else 'تحته'}\nRSI: {l['rsi']:.0f}\nATR: {s5.atr:.2f}\nالجلسة: {T.SESSION.get(ctx.session['label'],ctx.session['label'])}\nالدرجة: {c.confluence:.0f}\nالإشارة: {T.DIR[b.signal] if b and b.signal != 'NONE' else 'لا يوجد'}\nالقرار: {T.final_ar(c.log.final)}"
    def signal(args):
        c=app.last_cand
        if c is None: return "لم تتم أي دورة تحليل بعد."
        if c.best is None or c.best.signal == "NONE": return "لا يوجد إعداد الآن."
        p=c.best.plan; return f"إعداد مرشّح: {T.DIR[p.direction]} ({T.STRATEGY.get(c.best.name,c.best.name)})\nالدخول {p.entry} | SL {p.sl}\nTP1 {p.tp1} | TP2 {p.tp2}\nRR 1:{p.rr2} | الدرجة {c.confluence:.0f}\nالقرار: {T.final_ar(c.log.final)}"
    def stats(args):
        s=app.journal.stats()
        return f"الإشارات {s['total']} | المفتوحة {s['open']} | المغلقة {s['closed']}" if not s.get('closed') else f"الإشارات {s['total']} | المغلقة {s['closed']}\nنسبة الربح {s['win_rate']:.1%} | متوسط R {s['avg_r']:.2f} | إجمالي R {s['total_r']:.2f}"
    def history(args):
        rows=app.journal.last_signals(10)
        return "لا توجد إشارات بعد." if not rows else "\n".join(f"#{r['id']} {T.DIR[r['direction']]} {r['entry']:.2f} ← {T.RESULT.get(r['result'] or r['status'],r['result'] or r['status'])}" for r in rows)
    def last(args):
        rows=app.journal.last_signals(1)
        if not rows: return "لا توجد إشارات بعد."
        r=rows[0]; return f"#{r['id']} {T.DIR[r['direction']]}\nالدخول {r['entry']:.2f} | SL {r['sl']:.2f} | TP1 {r['tp1']:.2f} | TP2 {r['tp2']:.2f}\nالحالة: {T.RESULT.get(r['result'] or r['status'],r['result'] or r['status'])}"
    def market(args):
        c=app.last_cand
        if c is None or c.ctx is None: return "لا يوجد تحليل بعد."
        ctx=c.ctx; return "\n".join([f"السوق {ctx.mid:.2f}",f"الاتجاه العام: {T.TREND.get(ctx.mtf['bias'],ctx.mtf['bias'])}"]+[f"{tf}: {T.TREND[ctx.tf[tf].trend]} ATR {ctx.tf[tf].atr:.2f}" for tf in ("4H","1H","15m","5m","1m")])
    def session(args):
        c=app.last_cand
        return "لا يوجد تحليل بعد." if c is None or c.ctx is None else f"الجلسة: {T.SESSION.get(c.ctx.session['label'],c.ctx.session['label'])}\nالأعلى {c.ctx.session['high']} | الأدنى {c.ctx.session['low']}"
    def train(args):
        if app.bootstrap.running: return "التهيئة تعمل الآن."
        if app.loop is None: return "النظام لم يكتمل تشغيله بعد."
        import asyncio; asyncio.run_coroutine_threadsafe(app.bootstrap.run(True),app.loop); return "بدأت إعادة بناء السجل التاريخي في الخلفية."
    return {"/start":help_,"/help":help_,"/status":status,"/price":price,"/analysis":analysis,"/signal":signal,"/stats":stats,"/history":history,"/last":last,"/market":market,"/session":session,"/train":train}

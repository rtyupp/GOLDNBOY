"""مساعد Gemini داخل تيليجرام: يفهم أي طلب بالعربي ويستخدم أدوات البوت (قراءة فقط):
السعر، التحليل، الأخبار، الشارت (صورة)، الإحصائيات، الإشارات، الحالة، وسبب عدم وجود صفقة.
يقبل أيضًا صورة تبعثها أنت (شارت/لقطة) ليحللها مع بيانات البوت الحية.
لا يستطيع تنفيذ صفقات ولا تغيير الإعدادات."""
from __future__ import annotations
import asyncio, base64, json, logging
from collections import defaultdict, deque
from typing import Optional
import pandas as pd
from goldbot import i18n as T
from goldbot.models import RiskPlan

log = logging.getLogger("assistant")

SYSTEM = (
    "أنت مساعد بوت تداول الذهب (XAUUSD) داخل تيليجرام. تجيب دائمًا بالعربية، بإيجاز يناسب شاشة الجوال، بنص عادي بدون ماركداون أو جداول.\\n"
    "استخدم الأدوات المتاحة لأي معلومة عن السعر أو التحليل أو الأخبار أو الشارت أو الإحصائيات؛ لا تخترع أي رقم ولا تعتمد على ذاكرتك للأسعار.\\n"
    "إذا طلب المستخدم صورة الشارت فاستدع get_chart (تُرسل الصورة له تلقائيًا) ثم علّق عليها باختصار.\\n"
    "إذا أرسل المستخدم صورة، حلّلها واربطها ببيانات البوت الحية عبر الأدوات.\\n"
    "أنت للقراءة والتحليل فقط: لا تنفّذ صفقات ولا تغيّر الإعدادات، وإن طُلب منك ذلك فاشرح أنك لا تستطيع.\\n"
    "قرار الإشارات يصدر من محرك البوت وفلاتره لا منك؛ إن سُئلت عن سبب غياب صفقة فاستدع explain_no_trade.\\n"
    "ذكّر بأن هذا ليس نصيحة مالية فقط عند السؤال عن قرار شراء/بيع شخصي."
).replace("\\n", "\n")

_NOARGS = {"type": "object", "properties": {}}
TOOLS = [{"functionDeclarations": [
    {"name": "get_price", "description": "السعر اللحظي للذهب: Bid/Ask/السبريد وعمر آخر تحديث وهل البيانات حديثة."},
    {"name": "get_analysis", "description": "التحليل الحالي الكامل: اتجاه كل فريم، الهيكل، السيولة، VWAP، RSI، ATR، الجلسة، المستويات المهمة، وآخر قرار للبوت."},
    {"name": "get_news", "description": "حالة الأخبار الاقتصادية المهمة (للدولار) والأحداث القادمة مع التوقع والسابق، وهل التداول ممنوع بسببها الآن.",
     "parameters": {"type": "object", "properties": {"count": {"type": "integer", "description": "عدد الأحداث القادمة (افتراضي 8)"}}}},
    {"name": "get_chart", "description": "يرسل للمستخدم صورة شارت الذهب مع المتوسطات وVWAP ومناطق العرض والطلب والسيولة ومستويات الصفقة المفتوحة إن وُجدت.",
     "parameters": {"type": "object", "properties": {"timeframe": {"type": "string", "enum": ["1m", "5m", "15m", "1H", "4H"], "description": "الفريم (افتراضي 5m)"}}}},
    {"name": "get_stats", "description": "إحصائيات الإشارات المغلقة: نسبة الربح، متوسط R، معامل الربح، حسب الاستراتيجية والجلسة."},
    {"name": "get_signals", "description": "آخر الإشارات المرسلة ونتائجها.",
     "parameters": {"type": "object", "properties": {"count": {"type": "integer", "description": "العدد (افتراضي 5)"}}}},
    {"name": "get_status", "description": "حالة النظام: الاتصال اللحظي، البيانات، تقويم الأخبار، الذكاء الاصطناعي، آخر دورة تحليل."},
    {"name": "explain_no_trade", "description": "يشرح بالتفصيل لماذا لا توجد صفقة الآن: ما ينقص كل استراتيجية والفلاتر التي منعت."},
]}]


class Assistant:
    def __init__(self, app, max_steps: int = 6, history_turns: int = 6):
        self.app = app
        self.max_steps = max_steps
        self.history = defaultdict(lambda: deque(maxlen=history_turns * 2))

    # ------------------------------------------------------------------ الأدوات
    def _h(self, cmd: str) -> str:
        return self.app.cmd.handlers[cmd]([]) if self.app.cmd else "غير متاح"

    def chart_png(self, tf: str = "5m") -> Optional[bytes]:
        from goldbot.notify.chart import render_chart
        c = self.app.last_cand
        if c is None or c.ctx is None:
            return None
        plan = None
        op = self.app.journal.open_trades()
        if op:
            r = op[0]
            risk = abs(r["entry"] - r["sl"])
            plan = RiskPlan(r["direction"], r["entry"], r["sl"], r["tp1"], r["tp2"], risk, abs(r["tp1"] - r["entry"]),
                            abs(r["tp2"] - r["entry"]), r["rr1"], r["rr2"])
        return render_chart(c.ctx, plan, tf if tf in c.ctx.tf else "5m", int(self.app.cfg.get("telegram.chart_bars", 100)))

    def run_tool(self, name: str, args: dict):
        """يرجع (نتيجة قابلة للتحويل إلى JSON, قائمة صور PNG لإرسالها للمستخدم)."""
        try:
            if name == "get_price":
                return self._h("/price"), []
            if name == "get_analysis":
                return {"analysis": self._h("/analysis"), "market": self._h("/market"), "session": self._h("/session")}, []
            if name == "get_news":
                now = pd.Timestamp.now(tz="UTC")
                st = self.app.news.status(now)
                return {"state": T.NEWS_STATE[st.state], "event": st.event, "minutes": st.minutes, "calendar_ok": st.calendar_ok,
                        "source": self.app.news.source, "last_update": str(self.app.news.last_ok),
                        "upcoming": self.app.news.next_events_full(now, int(args.get("count") or 8))}, []
            if name == "get_chart":
                tf = str(args.get("timeframe") or "5m")
                png = self.chart_png(tf)
                if png is None:
                    return {"error": "لا يوجد تحليل جاهز بعد لرسم الشارت (انتظر دورة التحليل الأولى)"}, []
                return {"ok": True, "note": f"أُرسلت صورة الشارت ({tf}) للمستخدم وهي مرفقة لك أيضًا لتحللها"}, [png]
            if name == "get_stats":
                return self._h("/stats"), []
            if name == "get_signals":
                return self.app.cmd.handlers["/history"]([]) if not args.get("count") else self._signals(int(args["count"])), []
            if name == "get_status":
                return self._h("/status"), []
            if name == "explain_no_trade":
                return self._h("/signal"), []
        except Exception as e:
            log.exception("فشل أداة %s", name)
            return {"error": f"{type(e).__name__}: {e}"}, []
        return {"error": f"أداة غير معروفة: {name}"}, []

    def _signals(self, n: int) -> str:
        rows = self.app.journal.last_signals(max(1, min(n, 15)))
        return "\n".join(f"#{r['id']} {r['timestamp'][5:16]} {T.DIR[r['direction']]} {r['entry']:.2f} {T.STRATEGY.get(r['strategy'], r['strategy'])} ← "
                         f"{T.RESULT.get(r['result'] or r['status'], r['result'] or r['status'])}" + (f" ({r['r']:+.2f}R)" if r['r'] is not None else "") for r in rows) or "لا توجد إشارات."

    # ------------------------------------------------------------------ المحادثة
    @staticmethod
    def _img(png: bytes) -> dict:
        return {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(png).decode()}}

    async def answer(self, chat_id, text: str, image: Optional[bytes] = None, image_mime: str = "image/jpeg"):
        """يرجع (نص الرد, [صور PNG للإرسال])."""
        g = self.app.gemini
        if not g.enabled:
            return "طبقة الذكاء الاصطناعي معطّلة في الإعدادات (ai.enabled). استخدم الأوامر مثل /status و /analysis.", []
        if g.adapter is not None:
            return await self._answer_with_adapter(chat_id, text), []
        text = (text or "").strip() or "حلّل هذه الصورة."
        hist = list(self.history[chat_id])
        parts = [{"text": text}]
        if image:
            parts.append({"inlineData": {"mimeType": image_mime, "data": base64.b64encode(image).decode()}})
        contents = hist + [{"role": "user", "parts": parts}]
        photos: list = []
        final = ""
        try:
            for _ in range(self.max_steps):
                body = {"systemInstruction": {"parts": [{"text": SYSTEM}]}, "contents": contents, "tools": TOOLS,
                        "generationConfig": {"temperature": 0.3}}
                try:
                    resp = await asyncio.to_thread(g.generate, body)
                except Exception as e:
                    if "400" in str(e):                    # غالبًا مشكلة في الصور المرفقة: نعيد المحاولة بدونها
                        contents = self._strip_images(contents)
                        body["contents"] = contents
                        resp = await asyncio.to_thread(g.generate, body)
                    else:
                        raise
                cand = resp["candidates"][0].get("content", {})
                parts_out = cand.get("parts", [])
                calls = [p["functionCall"] for p in parts_out if "functionCall" in p]
                if not calls:
                    final = "".join(p.get("text", "") for p in parts_out if "text" in p and not p.get("thought")).strip()
                    break
                contents.append(cand)                       # كما هي (تحافظ على توقيعات التفكير)
                resp_parts, imgs_for_model = [], []
                for c in calls:
                    result, imgs = self.run_tool(c.get("name", ""), c.get("args") or {})
                    resp_parts.append({"functionResponse": {"name": c.get("name"), "response": {"result": result}}})
                    photos.extend(imgs)
                    imgs_for_model.extend(self._img(i) for i in imgs)
                contents.append({"role": "user", "parts": resp_parts + imgs_for_model})
            if not final:
                final = "تمت معالجة طلبك." if photos else "لم أستطع تكوين رد مناسب، جرّب إعادة صياغة السؤال أو استخدم /help."
        except Exception as e:
            log.error("فشل المساعد: %s", e)
            return f"تعذّر الاتصال بالذكاء الاصطناعي الآن ({type(e).__name__}). جرّب الأوامر: /status /price /analysis /news /help", photos
        self.history[chat_id].append({"role": "user", "parts": [{"text": text}]})
        self.history[chat_id].append({"role": "model", "parts": [{"text": final}]})
        return final, photos

    @staticmethod
    def _strip_images(contents):
        out = []
        for c in contents:
            ps = [p for p in c.get("parts", []) if "inlineData" not in p]
            if ps:
                out.append({**c, "parts": ps})
        return out

    async def _answer_with_adapter(self, chat_id, text: str) -> str:
        """دالتك الحالية (نص فقط): نمرر لها بيانات البوت الحية داخل الـ prompt."""
        ctx = "\n\n".join([self._h("/price"), self._h("/analysis"), self._h("/news"), self._h("/status")])
        prompt = f"{SYSTEM}\n\nبيانات البوت الحية:\n{ctx}\n\nسؤال المستخدم: {text}"
        try:
            r = self.app.gemini.adapter(prompt)
            if asyncio.iscoroutine(r):
                r = await r
            return str(r)
        except Exception as e:
            return f"تعذّر الاتصال بالذكاء الاصطناعي ({type(e).__name__})."

"""مساعد Gemini داخل تيليجرام — سريع وموثوق:
 • استدعاء واحد لـ Gemini يحمل بيانات البوت الحية (سعر/تحليل/أخبار/آخر قرار) بدل حلقة أدوات بطيئة.
 • طلب الشارت يُنفَّذ محليًا فورًا (الصورة تصل قبل رد الذكاء الاصطناعي) ثم يعلّق عليها Gemini.
 • أي بطء أو فشل (429 / مهلة / إيقاف نموذج) ينتهي برد محلي من بيانات البوت، فلا يبقى طلبك بلا جواب.
 • قراءة وتحليل فقط: لا صفقات ولا تغيير إعدادات."""
from __future__ import annotations
import asyncio, base64, logging, re
from collections import defaultdict, deque
from typing import Optional
import pandas as pd
from goldbot.models import RiskPlan
from goldbot.ai.gemini_layer import GeminiHTTPError

log = logging.getLogger("assistant")

SYSTEM = (
    "أنت مساعد بوت تداول الذهب (XAUUSD) داخل تيليجرام. أجب دائمًا بالعربية وبإيجاز يناسب الجوال (حتى 8 أسطر)، بنص عادي بدون ماركداون أو نجوم أو عناوين.\n"
    "ستجد مع رسالة المستخدم «بيانات البوت الحية» (سعر، تحليل، أخبار، آخر قرار). اعتمد عليها حصرًا في أي رقم أو معلومة؛ إن لم تجد المعلومة قل ذلك ولا تخترع.\n"
    "قد تُرفق صورة شارت من البوت أو من المستخدم: صِف ما تراه وربطه بالبيانات.\n"
    "أنت للقراءة والتحليل فقط: لا تنفّذ صفقات ولا تغيّر الإعدادات؛ وإن طُلب ذلك فاشرح أنك لا تستطيع.\n"
    "قرار الإشارات يصدر من محرك البوت وفلاتره لا منك؛ فسّر أسباب غياب الصفقة من قسم «آخر قرار».\n"
    "اذكر أن هذا ليس نصيحة مالية فقط إذا سُئلت عن قرار شراء/بيع شخصي."
)

_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
_CHART_WORDS = ("شارت", "الشارت", "صورة", "صوره", "رسم", "chart", "screenshot", "لقطة", "لقطه", "بياني")


def parse_chart_request(text: str) -> Optional[str]:
    """يرجع الفريم إن كان الطلب يخص صورة الشارت، وإلا None."""
    t = (text or "").lower().translate(_AR_DIGITS)
    if not any(w in t for w in _CHART_WORDS):
        return None
    if re.search(r"(4\s*(h|ساع)|اربع ساعات|أربع ساعات|4س)", t):
        return "4H"
    if re.search(r"\b15\b|ربع ساعة|ربع ساعه", t):
        return "15m"
    if re.search(r"\b5\b|خمس دقائق|خمسة دقائق", t):
        return "5m"
    if re.search(r"(1\s*h\b|ساعة|ساعه|60)", t):
        return "1H"
    if re.search(r"(1\s*m\b|دقيقة|دقيقه)", t):
        return "1m"
    return "5m"


def clean(text: str) -> str:
    t = re.sub(r"\*\*|__|`", "", text or "")
    t = re.sub(r"^#+\s*", "", t, flags=re.M)
    return t.strip()


class Assistant:
    def __init__(self, app, history_turns: int = 6):
        self.app = app
        self.history = defaultdict(lambda: deque(maxlen=history_turns * 2))

    # ------------------------------------------------------------------ بيانات البوت
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

    def live_context(self, text: str) -> str:
        t = (text or "").lower()
        parts = [f"الوقت الآن: {pd.Timestamp.now(tz='UTC'):%Y-%m-%d %H:%M} UTC",
                 "== السعر ==\n" + self._h("/price"), "== التحليل ==\n" + self._h("/analysis"),
                 "== الأخبار ==\n" + self._h("/news"), "== آخر قرار ==\n" + self._h("/signal")]
        if any(k in t for k in ("احصائ", "إحصائ", "نتائج", "ربح", "خسار", "صفقات", "stats", "اداء", "أداء")):
            parts += ["== الإحصائيات ==\n" + self._h("/stats"), "== آخر الإشارات ==\n" + self._h("/history")]
        if any(k in t for k in ("حالة", "شغال", "status", "اتصال", "ذكاء", "تعلم", "ml")):
            parts.append("== حالة النظام ==\n" + self._h("/status"))
        return "\n\n".join(parts)

    def local_summary(self) -> str:
        return "\n\n".join([self._h("/price"), self._h("/analysis"), self._h("/news")])

    # ------------------------------------------------------------------ المحادثة
    async def answer(self, chat_id, text: str, image: Optional[bytes] = None, image_mime: str = "image/jpeg", send_photo=None):
        """يرجع (نص الرد, [صور PNG لم تُرسل بعد]). send_photo: async(png) لإرسال الشارت فورًا قبل رد الذكاء الاصطناعي."""
        g = self.app.gemini
        text = (text or "").strip()
        photos: list = []
        tf = parse_chart_request(text) if text else None
        chart = None
        if tf:
            chart = await asyncio.to_thread(self.chart_png, tf)
            if chart is None:
                return "لا يوجد تحليل جاهز بعد لرسم الشارت، انتظر دقيقة حتى تكتمل أول دورة تحليل ثم أعد الطلب.", []
            if send_photo is not None:
                await send_photo(chart)
            else:
                photos.append(chart)
        if not g.enabled:
            return ("الذكاء الاصطناعي معطّل في الإعدادات.\n\n" + self.local_summary()) if not tf else "تفضّل الشارت.", photos
        if g.adapter is not None:
            return await self._with_adapter(text), photos
        context = self.live_context(text)
        parts = [{"text": f"بيانات البوت الحية:\n{context}\n\nرسالة المستخدم: {text or 'حلّل هذه الصورة.'}"}]
        images = []
        if image:
            images.append({"inlineData": {"mimeType": image_mime, "data": base64.b64encode(image).decode()}})
        if chart:
            images.append({"inlineData": {"mimeType": "image/png", "data": base64.b64encode(chart).decode()}})
        contents = list(self.history[chat_id]) + [{"role": "user", "parts": parts + images}]
        body = {"systemInstruction": {"parts": [{"text": SYSTEM}]}, "contents": contents,
                "generationConfig": {"temperature": 0.3, "maxOutputTokens": 900}}
        try:
            final = await self._call(g, body, bool(images))
        except Exception as e:
            log.error("المساعد: فشل الرد الذكي (%s: %s)", type(e).__name__, e)
            note = "⚠️ الذكاء الاصطناعي لم يرد الآن (" + ("مهلة" if isinstance(e, asyncio.TimeoutError) else type(e).__name__) + ")."
            if tf:
                return note + " الشارت أُرسل أعلاه.", photos
            return note + " هذا ملخص من بيانات البوت:\n\n" + self.local_summary(), photos
        self.history[chat_id].append({"role": "user", "parts": [{"text": text or "(صورة)"}]})
        self.history[chat_id].append({"role": "model", "parts": [{"text": final}]})
        return final, photos

    async def _call(self, g, body: dict, has_images: bool) -> str:
        async def once(b):
            resp = await asyncio.wait_for(asyncio.to_thread(g.generate, b, g.chat_timeout, g.chat_thinking), g.chat_timeout * 2 + 5)
            cands = resp.get("candidates") or []
            if not cands:
                raise RuntimeError("رد فارغ (قد يكون محجوبًا بسبب السلامة)")
            ps = cands[0].get("content", {}).get("parts", [])
            return clean("".join(p.get("text", "") for p in ps if "text" in p and not p.get("thought")))
        try:
            out = await once(body)
        except GeminiHTTPError as e:
            if e.status == 400 and has_images:           # مشكلة غالبًا في الصورة: أعد بدونها
                stripped = {**body, "contents": [{**c, "parts": [p for p in c["parts"] if "inlineData" not in p]} for c in body["contents"]]}
                out = await once(stripped)
            else:
                raise
        if not out:
            raise RuntimeError("رد فارغ")
        return out

    async def _with_adapter(self, text: str) -> str:
        """دالتك الحالية (نص فقط): تُمرَّر لها البيانات الحية داخل النص."""
        prompt = f"{SYSTEM}\n\nبيانات البوت الحية:\n{self.live_context(text)}\n\nسؤال المستخدم: {text}"
        try:
            r = self.app.gemini.adapter(prompt)
            if asyncio.iscoroutine(r):
                r = await asyncio.wait_for(r, self.app.gemini.chat_timeout * 2)
            return clean(str(r))
        except Exception as e:
            return f"⚠️ الذكاء الاصطناعي لم يرد ({type(e).__name__}). هذا ملخص من البوت:\n\n" + self.local_summary()

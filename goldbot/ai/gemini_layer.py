"""طبقة المراجعة النهائية = Gemini.  النموذج الافتراضي: gemini-3.8-flash (أقوى نموذج مجاني حسب صفحة أسعار Google، سبتمبر 2026)
ويُضبط من config: ai.model أو متغير GEMINI_MODEL. البديل التلقائي عند الإيقاف: ai.fallback_model (الافتراضي gemini-3.7-flash).

خياران للربط:
 أ) ai.adapter: "your_module:your_function"  -> دالتك الحالية كما هي (def f(prompt:str)->str, عادية أو async).
 ب) استدعاء REST مباشر بـ GEMINI_API_KEY + النموذج أعلاه.

Gemini يراجع بطاقة التحليل فقط ويرد BUY / SELL / NO TRADE؛ لا يستطيع تغيير الدخول أو SL أو TP.

ملاحظة: نماذج Gemini 2.5 مجدولة للإيقاف في 16 أكتوبر 2026 (404 بعدها). عند اكتشاف إيقاف أي نموذج
يرسل البوت تنبيهًا عربيًا، ويستخدم ai.fallback_model إن وُجد، وإلا يكون القرار «لا صفقة».
الطبقة المجانية: محتوى الطلبات قد تستخدمه Google لتحسين منتجاتها (البطاقة تحتوي تحليل سوق فقط، بلا بيانات شخصية).
"""
from __future__ import annotations
import asyncio, importlib, json, logging, os, re
from dataclasses import dataclass
from typing import Callable, Optional
import requests

log = logging.getLogger("ai")
DEFAULT_MODEL = "gemini-3.8-flash"

SYSTEM = (
    "أنت المراجع النهائي للمخاطر في نظام تداول الذهب (XAUUSD). تستلم بطاقة تحليل محسوبة مسبقًا. "
    "القواعد: لا تخترع ولا تعدّل الدخول أو وقف الخسارة أو الأهداف؛ احكم فقط بما في البطاقة؛ "
    "إذا كانت البيانات متناقضة أو الإعداد ضعيف أو السوق غير واضح فالجواب NO TRADE. "
    "درجة التقاطع ليست احتمال ربح. اكتب السبب بالعربية في جملة قصيرة (25 كلمة كحد أقصى). "
    'أجب بـ JSON فقط بهذا الشكل: {"decision":"BUY|SELL|NO TRADE","reason":"..."}'
)

CHAT_SYSTEM = (
    "أنت مساعد GOLDNBOY العربي لشرح كود وبوت تداول الذهب. افهم خريطة المشروع والسوق المرفقة، "
    "وأجب بوضوح وباختصار مفيد. يمكنك شرح أي وحدة أو أمر أو مؤشر أو شارت، لكن لا تدّعي أنك نفذت "
    "كودًا أو جلبت سعرًا غير موجود في السياق، ولا تعدّل entry/sl/tp ولا تنفذ صفقة. إذا طلب المستخدم "
    "شارتًا فاذكر أن أمر /chart يرسل الصورة. لا تقدم ضمان ربح، واذكر نقص البيانات عند الحاجة."
)


class ModelRetired(RuntimeError):
    pass


class GeminiHTTPError(RuntimeError):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"HTTP {status}: {detail}")


@dataclass
class AIDecision:
    decision: str          # BUY | SELL | NO TRADE
    reason: str
    ok: bool = True        # False عند فشل الاستدعاء
    raw: str = ""


def parse_decision(text: str) -> AIDecision:
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?|```$", "", t.strip(), flags=re.M).strip()
    try:
        j = json.loads(t)
        d = str(j.get("decision", "")).upper().replace("_", " ").strip()
        if d in ("BUY", "SELL", "NO TRADE"):
            return AIDecision(d, str(j.get("reason", ""))[:200], True, text)
    except (ValueError, AttributeError):
        pass
    m = re.search(r'"decision"\s*:\s*"(BUY|SELL|NO[ _]TRADE)"', t, re.I)
    if m:
        return AIDecision(m.group(1).upper().replace("_", " "), "تم استخراج القرار من رد غير منسّق", True, text)
    return AIDecision("NO TRADE", "رد الذكاء الاصطناعي غير مفهوم", False, text)


class GeminiLayer:
    def __init__(self, cfg, session: requests.Session | None = None):
        self.enabled = bool(cfg.get("ai.enabled", True))
        self.on_error = cfg.get("ai.on_error", "block")      # block (لا صفقة عند الخطأ) | pass
        self.timeout = float(cfg.get("ai.timeout_sec", 30))
        self.model = os.environ.get("GEMINI_MODEL") or cfg.get("ai.model") or DEFAULT_MODEL
        self.fallback_model = os.environ.get("GEMINI_FALLBACK_MODEL") or cfg.get("ai.fallback_model")
        self.adapter: Optional[Callable] = None
        spec = cfg.get("ai.adapter")
        if spec:
            mod, fn = spec.split(":")
            self.adapter = getattr(importlib.import_module(mod), fn)
        self.s = session or requests.Session()
        self.calls = 0
        self.alert: Callable[[str], None] = lambda text: None     # يضبطه التطبيق لإرسال تنبيه
        self._alerted = False

    async def _ask(self, prompt: str) -> str:
        if self.adapter is not None:
            r = self.adapter(prompt)
            if asyncio.iscoroutine(r):
                r = await r
            return str(r)
        return await asyncio.to_thread(self._rest_with_fallback, prompt)

    def _rest_with_fallback(self, prompt: str) -> str:
        try:
            return self._rest(prompt, self.model)
        except ModelRetired:
            self._alert_once(f"⚠️ نموذج Gemini «{self.model}» لم يعد متاحًا (أوقفته Google). "
                             + (f"سأستخدم النموذج البديل «{self.fallback_model}»." if self.fallback_model
                                else "القرار الآن «لا صفقة» حتى تحدد نموذجًا بديلًا في ai.model أو GEMINI_MODEL."))
            if self.fallback_model:
                return self._rest(prompt, self.fallback_model)
            raise

    def _alert_once(self, text: str):
        log.error(text)
        if not self._alerted:
            self._alerted = True
            self.alert(text)

    def _rest(self, prompt: str, model: str) -> str:
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("حدد ai.adapter في config.yaml أو ضع GEMINI_API_KEY")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        body = {"systemInstruction": {"parts": [{"text": SYSTEM}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"}}
        r = self.s.post(url, json=body, headers={"x-goog-api-key": key}, timeout=self.timeout)
        if r.status_code == 404:
            raise ModelRetired(model)
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]

    async def decide(self, card: str, candidate: str) -> AIDecision:
        if not self.enabled:
            return AIDecision(candidate, "طبقة الذكاء الاصطناعي معطّلة في الإعدادات", True)
        prompt = f"{SYSTEM}\n\nبطاقة التحليل:\n{card}\n\nأجب بـ JSON فقط."
        try:
            self.calls += 1
            raw = await asyncio.wait_for(self._ask(prompt), timeout=self.timeout + 5)
            return parse_decision(raw)
        except ModelRetired:
            return AIDecision("NO TRADE", f"نموذج Gemini «{self.model}» متوقف", False)
        except Exception as e:
            log.error("فشل استدعاء Gemini: %s", e)
            if self.on_error == "pass":
                return AIDecision(candidate, f"تم تجاهل خطأ الذكاء الاصطناعي: {e}", False)
            return AIDecision("NO TRADE", f"خطأ في الذكاء الاصطناعي ({type(e).__name__})", False)

    async def chat(self, question: str, context: str) -> str:
        """محادثة تفسيرية منفصلة عن بوابة قرار الإشارة."""
        if not self.enabled:
            return "المساعد معطّل حاليًا. فعّل ai.enabled وضع GEMINI_API_KEY أو ai.adapter."
        prompt = f"{CHAT_SYSTEM}\n\nالسياق:\n{context[:14000]}\n\nسؤال المستخدم:\n{question[:2000]}"
        try:
            self.calls += 1
            raw = await asyncio.wait_for(self._chat_ask(prompt), timeout=self.timeout + 5)
            return raw.strip()[:3900] or "لم يصل رد مفهوم من المساعد."
        except ModelRetired:
            return f"نموذج Gemini «{self.model}» غير متاح لمفتاحك. غيّر GEMINI_MODEL إلى نموذج ظاهر في /models أو فعّل الاكتشاف التلقائي."
        except GeminiHTTPError as exc:
            log.error("فشل مساعد المحادثة: %s", exc)
            return self._http_ar(exc)
        except Exception as exc:
            log.error("فشل مساعد المحادثة: %s", exc)
            return f"تعذر تشغيل المساعد الآن ({type(exc).__name__}). تحقق من GEMINI_API_KEY ثم جرّب /status."

    async def _chat_ask(self, prompt: str) -> str:
        if self.adapter is not None:
            r = self.adapter(prompt)
            if asyncio.iscoroutine(r):
                r = await r
            return str(r)
        return await asyncio.to_thread(self._rest_chat_with_fallback, prompt)

    def _rest_chat_with_fallback(self, prompt: str) -> str:
        try:
            return self._rest_chat(prompt, self.model)
        except ModelRetired:
            if not self.fallback_model or self.fallback_model == self.model:
                raise
            self._alert_once(f"⚠️ نموذج Gemini «{self.model}» غير متاح؛ سأستخدم «{self.fallback_model}» للمحادثة.")
            return self._rest_chat(prompt, self.fallback_model)

    @staticmethod
    def _http_ar(exc: GeminiHTTPError) -> str:
        if exc.status in (401, 403):
            return "مفتاح Gemini مرفوض أو غير مصرح له. تأكد من GEMINI_API_KEY ومن تفعيل Gemini API في Google AI Studio."
        if exc.status == 429:
            return "تم تجاوز حد Gemini المجاني مؤقتًا. انتظر قليلًا أو استخدم نموذج Flash-Lite/مفتاحًا آخر."
        if exc.status == 400:
            return f"طلب Gemini غير مقبول: {exc.detail[:260]}"
        return f"خادم Gemini أعاد HTTP {exc.status}: {exc.detail[:260]}"

    def _rest_chat(self, prompt: str, model: str) -> str:
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("حدد ai.adapter أو ضع GEMINI_API_KEY")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        body = {"systemInstruction": {"parts": [{"text": CHAT_SYSTEM}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1200}}
        r = self.s.post(url, json=body, headers={"x-goog-api-key": key}, timeout=self.timeout)
        if r.status_code == 404:
            raise ModelRetired(model)
        if not r.ok:
            raise GeminiHTTPError(r.status_code, self._response_detail(r))
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]

    @staticmethod
    def _response_detail(r: requests.Response) -> str:
        try:
            return str(r.json().get("error", {}).get("message") or r.text)[:500]
        except ValueError:
            return r.text[:500]

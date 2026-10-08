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
import asyncio, base64, importlib, json, logging, os, re
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


class ModelRetired(RuntimeError):
    pass


class GeminiHTTPError(RuntimeError):
    def __init__(self, status: int, text: str = ""):
        super().__init__(f"HTTP {status}: {text[:200]}")
        self.status, self.text = status, text


TRANSIENT = {429, 500, 502, 503, 504}


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
        self._no_thinking = False
        self.review_thinking = cfg.get("ai.review_thinking_level", "medium")
        self.chat_thinking = cfg.get("ai.chat_thinking_level", "low")
        self.chat_timeout = float(cfg.get("ai.chat_timeout_sec", 25))

    async def _ask(self, prompt: str, image: Optional[bytes] = None) -> str:
        if self.adapter is not None:                 # دالتك الحالية: نص فقط
            r = self.adapter(prompt)
            if asyncio.iscoroutine(r):
                r = await r
            return str(r)
        return await asyncio.to_thread(self._rest, prompt, image)

    def _alert_once(self, text: str):
        log.error(text)
        if not self._alerted:
            self._alerted = True
            self.alert(text)

    def _post(self, model: str, body: dict, timeout: Optional[float] = None) -> dict:
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("حدد ai.adapter في config.yaml أو ضع GEMINI_API_KEY")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        r = self.s.post(url, json=body, headers={"x-goog-api-key": key}, timeout=timeout or self.timeout)
        if r.status_code == 404:
            raise ModelRetired(model)
        if r.status_code >= 400:
            raise GeminiHTTPError(r.status_code, getattr(r, "text", "") or "")
        return r.json()

    def _with_thinking(self, body: dict, level: Optional[str]) -> dict:
        if not level or self._no_thinking:
            return body
        b = dict(body)
        gc = dict(b.get("generationConfig") or {})
        gc["thinkingConfig"] = {"thinkingLevel": level}
        b["generationConfig"] = gc
        return b

    def generate(self, body: dict, timeout: Optional[float] = None, thinking: Optional[str] = None) -> dict:
        """استدعاء عام (صور/بحث) — متزامن، استدعه عبر to_thread.
        سلوك الموثوقية: عند 429/5xx ننتقل فورًا إلى النموذج البديل؛ وعند رفض إعداد التفكير نعيد بدونه؛ وعند إيقاف النموذج ننبّه."""
        models = [self.model] + ([self.fallback_model] if self.fallback_model and self.fallback_model != self.model else [])
        last: Optional[Exception] = None
        for i, m in enumerate(models):
            for attempt in (0, 1):
                try:
                    return self._post(m, self._with_thinking(body, thinking), timeout)
                except ModelRetired as e:
                    last = e
                    self._alert_once(f"⚠️ نموذج Gemini «{m}» لم يعد متاحًا (أوقفته Google). "
                                     + (f"سأستخدم النموذج البديل «{models[i + 1]}»." if i + 1 < len(models)
                                        else "حدد نموذجًا بديلًا في ai.model أو GEMINI_MODEL."))
                    break
                except GeminiHTTPError as e:
                    last = e
                    if e.status == 400 and thinking and not self._no_thinking and "thinking" in e.text.lower():
                        self._no_thinking = True          # هذا النموذج لا يدعم الإعداد: أعد بدونه
                        log.warning("النموذج %s رفض thinkingConfig؛ سيُستخدم بدونه", m)
                        continue
                    if e.status in TRANSIENT:
                        log.warning("Gemini %s أعاد %s ← %s", m, e.status, "النموذج البديل" if i + 1 < len(models) else "فشل")
                        break
                    raise
                except requests.RequestException as e:    # مهلة/شبكة
                    last = e
                    log.warning("Gemini %s: %s", m, type(e).__name__)
                    break
        raise last if last else RuntimeError("فشل استدعاء Gemini")

    def _rest(self, prompt: str, image: Optional[bytes] = None) -> str:
        parts = [{"text": prompt}]
        if image:
            parts.append({"inlineData": {"mimeType": "image/png", "data": base64.b64encode(image).decode()}})
        body = {"systemInstruction": {"parts": [{"text": SYSTEM}]},
                "contents": [{"role": "user", "parts": parts}],
                "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"}}
        resp = self.generate(body, thinking=self.review_thinking)
        return "".join(p.get("text", "") for p in resp["candidates"][0]["content"]["parts"] if "text" in p and not p.get("thought"))

    async def decide(self, card: str, candidate: str, image: Optional[bytes] = None) -> AIDecision:
        if not self.enabled:
            return AIDecision(candidate, "طبقة الذكاء الاصطناعي معطّلة في الإعدادات", True)
        prompt = f"{SYSTEM}\n\nبطاقة التحليل:\n{card}\n" + ("\nمرفق صورة الشارت: تحقق أن ما تراه فيها (الاتجاه، مناطق العرض/الطلب، سحب السيولة، مواضع الدخول والوقف والأهداف) يتفق مع البطاقة، وإلا فالجواب NO TRADE.\n" if image else "") + "\nأجب بـ JSON فقط."
        try:
            self.calls += 1
            raw = await asyncio.wait_for(self._ask(prompt, image), timeout=self.timeout + 5)
            return parse_decision(raw)
        except ModelRetired:
            return AIDecision("NO TRADE", f"نموذج Gemini «{self.model}» متوقف", False)
        except asyncio.TimeoutError:
            log.error("انتهت مهلة Gemini")
            return AIDecision(candidate if self.on_error == "pass" else "NO TRADE", "انتهت مهلة الذكاء الاصطناعي", False)
        except Exception as e:
            log.error("فشل استدعاء Gemini: %s", e)
            if self.on_error == "pass":
                return AIDecision(candidate, f"تم تجاهل خطأ الذكاء الاصطناعي: {e}", False)
            return AIDecision("NO TRADE", f"خطأ في الذكاء الاصطناعي ({type(e).__name__})", False)

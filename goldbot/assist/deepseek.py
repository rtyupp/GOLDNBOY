"""مساعد DeepSeek: يشرح ويلخّص ويجيب فقط. لا يدخل أبدًا في قرار الإشارة ولا يغيّر الدخول/الوقف/الأهداف.
 - يعمل تلقائيًا متى وُجد DEEPSEEK_API_KEY في البيئة؛ بدون مفتاح يصمت تمامًا ولا يطلب شيئًا من الشبكة.
 - أي فشل (مهلة، رصيد، حد معدل) يرجع None والبوت يكمل عمله كأن شيئًا لم يكن.
 - المفتاح لا يُكتب في سجل ولا في رسالة."""
from __future__ import annotations
import logging
import time
from typing import Optional
import requests

from goldbot.config import env

log = logging.getLogger("assist")

DISCLAIMER = "\n\n🤖 شرح آلي بواسطة DeepSeek — للمعلومة فقط وليس نصيحة مالية."

_RULES = ("أنت مساعد لبوت GOLDNBOY لإشارات الذهب XAUUSD. اكتب بالعربية الفصحى المبسطة وباختصار. "
          "اعتمد فقط على البيانات المعطاة لك في الرسالة؛ ممنوع اختراع أرقام أو أحداث أو أخبار، وإن لم تتوفر المعلومة قل ذلك صراحة. "
          "ممنوع تغيير مستويات الدخول/الوقف/الأهداف أو اقتراح بدائل لها. ممنوع الوعد بالربح أو قول إن الصفقة مضمونة. "
          "لا تكتب أي أرقام لم ترد في البيانات.")

SYS_EXPLAIN = _RULES + " المهمة: اشرح في 3 إلى 4 أسطر قصيرة لماذا اجتمعت شروط هذه الإشارة، واذكر في آخر سطر أهم خطر يبطلها."
SYS_SUMMARY = _RULES + " المهمة: اكتب تعليقًا من سطرين إلى 4 أسطر على أداء الفترة اعتمادًا على الأرقام فقط، مع ملاحظة صريحة إن كانت العينة صغيرة."
SYS_ASK = _RULES + " المهمة: أجب عن سؤال المستخدم من «بيانات البوت» فقط في 8 أسطر كحد أقصى. إن كان السؤال خارج هذه البيانات فاعتذر باختصار ووجّهه للأزرار."


class DeepSeek:
    def __init__(self, cfg):
        self.cfg = cfg
        self.url = str(cfg.get("assist.base_url", "https://api.deepseek.com")).rstrip("/") + "/chat/completions"
        self.model = env("DEEPSEEK_MODEL") or cfg.get("assist.model", "deepseek-v4-flash")
        self.timeout = float(cfg.get("assist.timeout_sec", 25))
        self.max_tokens = int(cfg.get("assist.max_tokens", 900))
        self.max_calls = int(cfg.get("assist.max_calls_per_day", 150))
        self._day, self._n = "", 0
        self._last_ask: dict = {}

    @property
    def enabled(self) -> bool:
        return bool(env("DEEPSEEK_API_KEY")) and bool(self.cfg.get("assist.enabled", True))

    def status_text(self) -> str:
        if not env("DEEPSEEK_API_KEY"):
            return "غير مفعّل (لا يوجد DEEPSEEK_API_KEY)"
        return f"يعمل ({self.model}) — استدعاءات اليوم {self._n}/{self.max_calls}"

    # ------------------------------------------------------------------ الاستدعاء
    def _budget_ok(self) -> bool:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if today != self._day:
            self._day, self._n = today, 0
        return self._n < self.max_calls

    def chat(self, system: str, user: str, max_tokens: Optional[int] = None) -> Optional[str]:
        if not self.enabled or not self._budget_ok():
            return None
        self._n += 1
        try:
            r = requests.post(self.url, timeout=self.timeout,
                              headers={"Authorization": f"Bearer {env('DEEPSEEK_API_KEY')}", "Content-Type": "application/json"},
                              json={"model": self.model, "stream": False, "temperature": 0.3,
                                    "max_tokens": max_tokens or self.max_tokens,
                                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
            if r.status_code != 200:
                log.warning("DeepSeek رفض الطلب: HTTP %s", r.status_code)      # لا نسجّل المحتوى ولا المفتاح
                return None
            text = (r.json()["choices"][0]["message"].get("content") or "").strip()
            return text[:1500] or None
        except Exception as e:
            log.warning("تعذّر الاتصال بـ DeepSeek: %s", type(e).__name__)
            return None

    # ------------------------------------------------------------------ 1) شرح الإشارة
    def explain_signal(self, facts: str) -> Optional[str]:
        out = self.chat(SYS_EXPLAIN, "بيانات الإشارة:\n" + facts)
        return (out + DISCLAIMER) if out else None

    # ------------------------------------------------------------------ 2) الملخص
    def comment_summary(self, facts: str) -> Optional[str]:
        out = self.chat(SYS_SUMMARY, "أرقام الفترة:\n" + facts, max_tokens=600)
        return (out + DISCLAIMER) if out else None

    # ------------------------------------------------------------------ 3) الأسئلة الحرة
    def ask(self, question: str, context: str, chat=None) -> Optional[str]:
        now = time.time()
        if now - self._last_ask.get(chat, 0) < 3:                    # حماية من الإغراق
            return "أرسل سؤالًا واحدًا كل بضع ثوانٍ من فضلك."
        self._last_ask[chat] = now
        out = self.chat(SYS_ASK, f"بيانات البوت:\n{context}\n\nسؤال المستخدم:\n{question[:500]}", max_tokens=700)
        return (out + DISCLAIMER) if out else None


def signal_facts(cand, plan, ctx, prob_txt: str, reason_ar: str, strategy_ar: str, session_ar: str, dir_ar: str) -> str:
    parts = ", ".join(f"{k}={v:.0f}" for k, v in (cand.conf_parts or {}).items())
    bias = ctx.mtf.get("bias") if getattr(ctx, "mtf", None) else None
    return (f"الاتجاه: {dir_ar}\nالاستراتيجية: {strategy_ar}\nالجلسة: {session_ar}\n"
            f"الدخول: {plan.entry}\nوقف الخسارة: {plan.sl}\nالهدف الأول: {plan.tp1}\nالهدف الثاني: {plan.tp2}\n"
            f"نسبة المخاطرة للعائد: {plan.rr1} / {plan.rr2}\nتقييم تقاطع الأدلة: {cand.confluence:.0f}/100 ({parts})\n"
            f"الاتجاه العام للفريمات الكبيرة: {bias}\nالاحتمال التاريخي: {prob_txt}\nمستوى المخاطرة: {cand.risk_level}\n"
            f"شروط تحققت: {reason_ar}")

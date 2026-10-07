"""تيليجرام عبر Bot API (requests داخل threads).
الإشارات تذهب إلى: private = محادثتك الخاصة مع البوت (TELEGRAM_ADMIN_IDS) | channel = القناة | both = الاثنين.
الأوامر تعمل فقط للأرقام الموجودة في TELEGRAM_ADMIN_IDS (ما عدا /id)."""
from __future__ import annotations
import asyncio, logging, time
from typing import Callable, List, Optional
import requests
from goldbot import i18n as T

log = logging.getLogger("telegram")
API = "https://api.telegram.org/bot{token}/{method}"


class Telegram:
    def __init__(self, token: str, channel_id: Optional[str], admin_ids: List[int], target: str = "private",
                 session: requests.Session | None = None):
        self.token, self.channel_id, self.admin_ids = token, channel_id, list(admin_ids)
        self.target = target
        self.s = session or requests.Session()
        self.sent = 0
        self.last_error: Optional[str] = None

    def _call(self, method: str, **kw):
        url = API.format(token=self.token, method=method)
        files = kw.pop("files", None)
        timeout = kw.pop("timeout", 30)
        for attempt in range(3):
            try:
                r = self.s.post(url, data=kw, files=files, timeout=timeout)
                j = r.json()
                if j.get("ok"):
                    return j["result"]
                if r.status_code == 429:
                    time.sleep(float(j.get("parameters", {}).get("retry_after", 2)) + 0.5)
                    continue
                self.last_error = f"{method}: {j.get('description')}"
                hint = " — افتح البوت في تيليجرام واضغط Start أولًا (البوت لا يستطيع بدء المحادثة معك)" if r.status_code == 403 else ""
                log.error("خطأ تيليجرام %s%s", self.last_error, hint)
                return None
            except (requests.RequestException, ValueError) as e:
                self.last_error = f"{method}: {e}"
                log.warning("تيليجرام: إعادة محاولة %d (%s)", attempt + 1, self.last_error)
                time.sleep(1.5 * (attempt + 1))
        return None

    def send_message(self, chat_id, text: str):
        r = self._call("sendMessage", chat_id=chat_id, text=text[:4000], disable_web_page_preview="true")
        self.sent += 1 if r else 0
        return r

    def send_photo(self, chat_id, png: bytes, caption: str = ""):
        r = self._call("sendPhoto", chat_id=chat_id, caption=caption[:1000], files={"photo": ("chart.png", png, "image/png")}, timeout=60)
        self.sent += 1 if r else 0
        return r

    def get_updates(self, offset: Optional[int], timeout: int = 25):
        return self._call("getUpdates", offset=offset if offset is not None else "", timeout=timeout,
                          allowed_updates='["message"]') or []

    # ---- وجهة الإشارات (خاص / قناة / الاثنان)
    def targets(self) -> list:
        t = []
        if self.target in ("private", "both"):
            t += self.admin_ids
        if self.target in ("channel", "both") and self.channel_id:
            t.append(self.channel_id)
        return t

    def send_signal(self, text: str) -> bool:
        ok = False
        for chat in self.targets():
            ok = bool(self.send_message(chat, text)) or ok
        return ok

    def send_signal_photo(self, png: bytes, caption: str) -> bool:
        ok = False
        for chat in self.targets():
            ok = bool(self.send_photo(chat, png, caption)) or ok
        return ok


def format_signal(plan, strategy: str, session: str, confidence: float, prob_text: str, reason: str) -> str:
    emoji = "🟢" if plan.direction == "BUY" else "🔴"
    return (f"الذهب XAU/USD\n\n{emoji} {T.DIR[plan.direction]}\n\n"
            f"الدخول:\n{plan.entry:.2f}\n\nوقف الخسارة (SL):\n{plan.sl:.2f}\n\n"
            f"الهدف الأول (TP1):\n{plan.tp1:.2f}\n\nالهدف الثاني (TP2):\n{plan.tp2:.2f}\n\n"
            f"العائد/المخاطرة:\n1:{plan.rr2:.1f}  (الهدف الأول 1:{plan.rr1:.1f})\n\n"
            f"درجة تقاطع الأدلة:\n{confidence:.0f}/100\n\nالاحتمال التاريخي:\n{prob_text}\n\n"
            f"الاستراتيجية:\n{T.STRATEGY.get(strategy, strategy)}\n\nالجلسة:\n{T.SESSION.get(session, session)}\n\nالسبب:\n{reason}\n\n"
            "⚠️ ليست نصيحة مالية. درجة تقاطع الأدلة ليست احتمال ربح؛ الاحتمال الحقيقي هو الاحتمال التاريخي فقط.")


class CommandBot:
    """استقبال الأوامر (long polling). handlers: '/cmd' -> دالة(args) ترجع نصًا."""
    PUBLIC = {"/id"}

    def __init__(self, tg: Telegram, handlers: dict):
        self.tg, self.handlers = tg, handlers
        self.offset: Optional[int] = None
        self._stop = False

    def stop(self):
        self._stop = True

    def handle_text(self, text: str) -> Optional[str]:
        parts = (text or "").strip().split()
        if not parts or not parts[0].startswith("/"):
            return None
        cmd = parts[0].split("@")[0].lower()
        fn = self.handlers.get(cmd)
        if fn is None:
            return "أمر غير معروف. جرّب /help"
        try:
            return fn(parts[1:])
        except Exception as e:
            log.exception("فشل تنفيذ الأمر %s", cmd)
            return f"حدث خطأ في الأمر {cmd}: {type(e).__name__}: {e}"

    def process_update(self, u: dict):
        m = u.get("message") or {}
        uid = (m.get("from") or {}).get("id")
        chat = (m.get("chat") or {}).get("id")
        if uid is None or chat is None:
            return
        text = m.get("text", "")
        cmd = (text.strip().split() or [""])[0].split("@")[0].lower()
        if cmd == "/id":                                  # مفتوح للجميع: ليعرف المستخدم رقمه ويضعه في الإعدادات
            self.tg.send_message(chat, f"رقم حسابك (TELEGRAM_ADMIN_IDS): {uid}\nرقم هذه المحادثة: {chat}")
            return
        if self.tg.admin_ids and uid not in self.tg.admin_ids:
            return                                        # تجاهل الغرباء بصمت
        reply = self.handle_text(text)
        if reply:
            self.tg.send_message(chat, reply)

    async def run(self):
        while not self._stop:
            ups = await asyncio.to_thread(self.tg.get_updates, self.offset, 25)
            for u in ups:
                self.offset = u["update_id"] + 1
                try:
                    await asyncio.to_thread(self.process_update, u)
                except Exception:
                    log.exception("فشل معالجة رسالة")
            if not ups:
                await asyncio.sleep(0.5)

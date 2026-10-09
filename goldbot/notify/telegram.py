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
        http_timeout = kw.pop("_http_timeout", None)
        if http_timeout is None:
            timeout = kw.pop("timeout", 30)          # ordinary call: "timeout" is the HTTP timeout
        else:
            timeout = http_timeout                   # long polling: "timeout" stays a Telegram API parameter
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

    def send_chat_action(self, chat_id, action: str = "typing"):
        return self._call("sendChatAction", chat_id=chat_id, action=action, timeout=10)

    def download_photo(self, message: dict) -> Optional[bytes]:
        """يحمّل أكبر نسخة من صورة أرسلها المستخدم."""
        photos = message.get("photo") or []
        if not photos:
            return None
        fid = max(photos, key=lambda p: p.get("file_size", 0) or p.get("width", 0)).get("file_id")
        info = self._call("getFile", file_id=fid, timeout=20)
        if not info:
            return None
        try:
            r = self.s.get(f"https://api.telegram.org/file/bot{self.token}/{info['file_path']}", timeout=40)
            return r.content if r.status_code == 200 else None
        except requests.RequestException:
            return None

    def get_updates(self, offset: Optional[int], timeout: int = 25):
        # real long polling: Telegram holds the request up to `timeout` s; the HTTP timeout must be longer
        kw = {"timeout": timeout, "_http_timeout": timeout + 15, "allowed_updates": '["message"]'}
        if offset is not None:
            kw["offset"] = offset
        return self._call("getUpdates", **kw) or []

    def delete_webhook(self) -> bool:
        """A registered webhook makes getUpdates fail with 409; clear it once at startup."""
        return self._call("deleteWebhook", drop_pending_updates="false", timeout=15) is not None

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
        self.ai = None          # async(chat_id, text, image, send_photo=) -> (نص, [صور]) تضبطها التطبيق
        self._locks: dict = {}
        self._tasks: set = set()

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

    typing_interval = 4.0
    ack_after = 8.0

    async def _typing_and_ack(self, chat):
        """يُبقي مؤشر «يكتب...» ويرسل «جارٍ التحليل» إن تأخر الرد."""
        t0 = 0.0
        acked = False
        while True:
            await asyncio.to_thread(self.tg.send_chat_action, chat)
            await asyncio.sleep(self.typing_interval)
            t0 += self.typing_interval
            if t0 >= self.ack_after and not acked:
                acked = True
                await asyncio.to_thread(self.tg.send_message, chat, "⏳ جارٍ التحليل، لحظات...")

    async def handle_update(self, u: dict):
        m = u.get("message") or {}
        uid = (m.get("from") or {}).get("id")
        chat = (m.get("chat") or {}).get("id")
        if uid is None or chat is None:
            return
        text = m.get("text") or m.get("caption") or ""
        has_photo = bool(m.get("photo"))
        first = (text.strip().split() or [""])[0]
        if first.startswith("/") and not has_photo:
            await asyncio.to_thread(self.process_update, u)
            return
        if self.tg.admin_ids and uid not in self.tg.admin_ids:
            return
        if self.ai is None or not (text.strip() or has_photo):
            return
        lock = self._locks.setdefault(chat, asyncio.Lock())
        async with lock:                                   # رسائل نفس المحادثة بالترتيب، ومحادثات مختلفة بالتوازي
            helper = asyncio.create_task(self._typing_and_ack(chat))
            try:
                img = await asyncio.to_thread(self.tg.download_photo, m) if has_photo else None

                async def send_photo(png):
                    await asyncio.to_thread(self.tg.send_photo, chat, png, "")
                reply, photos = await self.ai(chat, text, img, send_photo=send_photo)
            except Exception as e:
                log.exception("فشل المساعد")
                reply, photos = f"حدث خطأ غير متوقع ({type(e).__name__}). جرّب الأوامر: /status /price /analysis", []
            finally:
                helper.cancel()
            if reply:
                await asyncio.to_thread(self.tg.send_message, chat, reply)
            for png in photos:
                await asyncio.to_thread(self.tg.send_photo, chat, png, "")

    async def _safe(self, u: dict):
        try:
            await self.handle_update(u)
        except Exception:
            log.exception("فشل معالجة رسالة")

    async def run(self):
        dw = getattr(self.tg, "delete_webhook", None)
        if dw:
            await asyncio.to_thread(dw)
        conflicts = 0
        while not self._stop:
            self.tg.last_error = None
            ups = await asyncio.to_thread(self.tg.get_updates, self.offset, 25)
            le = getattr(self.tg, "last_error", None) or ""
            if "getUpdates" in le and "Conflict" in le:
                # Only one long-polling consumer per token is allowed. On Render the OLD instance keeps
                # running for a short while during a deploy -> wait and retry instead of giving up forever.
                conflicts += 1
                wait = min(60, 10 * conflicts)
                log.warning("تعارض getUpdates (نسخة أخرى تستقبل الأوامر؛ غالبًا نشر جديد على Render) — إعادة المحاولة بعد %ds", wait)
                await asyncio.sleep(wait)
                continue
            conflicts = 0
            for u in ups:
                self.offset = u["update_id"] + 1
                t = asyncio.create_task(self._safe(u))        # كل رسالة في مهمة مستقلة: الرد البطيء لا يوقف الاستقبال
                self._tasks.add(t)
                t.add_done_callback(self._tasks.discard)
            if le:                                            # network/API error: back off so we never spin
                await asyncio.sleep(3)
            elif not ups:
                await asyncio.sleep(0.2)

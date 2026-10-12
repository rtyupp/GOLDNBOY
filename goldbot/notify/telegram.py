"""تيليجرام عبر Bot API (requests داخل threads).
الإشارات تذهب إلى: private = محادثتك الخاصة مع البوت (TELEGRAM_ADMIN_IDS) | channel = القناة | both = الاثنين.
الأوامر تعمل فقط للأرقام الموجودة في TELEGRAM_ADMIN_IDS (ما عدا /id)."""
from __future__ import annotations
import asyncio, json, logging, re, time
from typing import List, Optional
import requests
from goldbot import i18n as T

log = logging.getLogger("telegram")
API = "https://api.telegram.org/bot{token}/{method}"

WELCOME = ("«ارحبو تراحيب المطر»\n\n"
           "بوت GOLDNBOY لإشارات تداول الذهب XAUUSD، يعتمد على الأسعار اللحظية والاستراتيجيات والمؤشرات الفنية "
           "لتحليل السوق وتحديد فرص التداول المحتملة.\n\n"
           "by: @QUOP9")

# ---- لوحة الأزرار السفلية (ReplyKeyboardMarkup). كل زر → أمر.
BTN_MORE, BTN_BACK = "⚙️ المزيد", "🔙 القائمة الرئيسية"
MAIN_ROWS = [
    ["💰 السعر اللحظي", "📈 التحليل الحالي"],
    ["🎯 آخر إعداد", "🌍 نظرة السوق"],
    ["📊 الإحصائيات", "🧾 سجل الإشارات"],
    [BTN_MORE],
]
MORE_ROWS = [
    ["🟢 حالة النظام", "🕒 الجلسة الحالية"],
    ["📌 آخر إشارة", "🛑 لماذا لا توجد توصية؟"],
    ["🧪 إعادة بناء السجل", BTN_BACK],
]
BUTTON_COMMANDS = {
    "💰 السعر اللحظي": "/price", "📈 التحليل الحالي": "/analysis", "🎯 آخر إعداد": "/signal", "🌍 نظرة السوق": "/market",
    "📊 الإحصائيات": "/stats", "🧾 سجل الإشارات": "/history",
    "🟢 حالة النظام": "/status", "🕒 الجلسة الحالية": "/session", "📌 آخر إشارة": "/last",
    "🛑 لماذا لا توجد توصية؟": "/why", "🧪 إعادة بناء السجل": "/train", "❓ المساعدة": "/help",
}
MENUS = {"main": MAIN_ROWS, "more": MORE_ROWS}
_NON_LETTERS = re.compile(r"[^\w\u0600-\u06FF]+", re.U)


def _norm(label: str) -> str:
    """يطابق نص الزر حتى لو اختلف الإيموجي أو المسافات."""
    return _NON_LETTERS.sub("", label or "").replace("_", "")


_BUTTON_NORM = {_norm(k): v for k, v in BUTTON_COMMANDS.items()}
_BUTTON_NORM[_norm(BTN_MORE)] = "@more"
_BUTTON_NORM[_norm(BTN_BACK)] = "@main"


def reply_keyboard(menu: str = "main") -> str:
    """JSON لـ ReplyKeyboardMarkup: تظهر مكان لوحة الكتابة، وتبقى ظاهرة (is_persistent)."""
    return json.dumps({"keyboard": [[{"text": t} for t in row] for row in MENUS.get(menu, MAIN_ROWS)],
                       "resize_keyboard": True, "is_persistent": True, "one_time_keyboard": False,
                       "input_field_placeholder": "اختر من الأزرار بالأسفل 👇"}, ensure_ascii=False)


def is_private_chat(chat_id) -> bool:
    """القنوات والمجموعات لا تدعم ReplyKeyboard: نرسلها فقط للمحادثات الخاصة (معرّف موجب)."""
    try:
        return int(chat_id) > 0
    except (TypeError, ValueError):
        return False


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

    def send_message(self, chat_id, text: str, menu: Optional[str] = None):
        """menu=None: لا يلمس اللوحة الحالية (مثلًا رسائل الإشارات). menu='main'|'more': يعرض/يبقي اللوحة السفلية."""
        kw = dict(chat_id=chat_id, text=text[:4000], disable_web_page_preview="true")
        if menu and is_private_chat(chat_id):
            kw["reply_markup"] = reply_keyboard(menu)
        r = self._call("sendMessage", **kw)
        self.sent += 1 if r else 0
        return r

    def send_photo(self, chat_id, png: bytes, caption: str = "", menu: Optional[str] = None):
        kw = dict(chat_id=chat_id, caption=caption[:1000], files={"photo": ("chart.png", png, "image/png")}, timeout=60)
        if menu and is_private_chat(chat_id):
            kw["reply_markup"] = reply_keyboard(menu)
        r = self._call("sendPhoto", **kw)
        self.sent += 1 if r else 0
        return r

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
            "⚠️ ليست نصيحة مالية. درجة تقاطع الأدلة ليست احتمال ربح؛ الاحتمال الحقيقي هو الاحتمال التاريخي فقط.\n\nby: QUOP9")


class CommandBot:
    """استقبال الأوامر والأزرار (long polling). handlers: '/cmd' -> دالة(args) ترجع نصًا."""
    PUBLIC = {"/id"}
    def __init__(self, tg: Telegram, handlers: dict):
        self.tg, self.handlers = tg, handlers
        self.offset: Optional[int] = None
        self._stop = False
        self.menu: dict = {}            # chat -> "main" | "more" (القسم الظاهر حاليًا)
        self.free_text = None           # اختياري: دالة(text, chat) -> رد أو None (أسئلة حرة للمساعد)
        self.chart_for_command = None   # callback اختياري: /price -> PNG bytes
        self._tasks: set = set()

    def stop(self):
        self._stop = True

    def menu_for(self, chat) -> str:
        return self.menu.get(chat, "main")

    def handle_text(self, text: str, chat=None) -> Optional[str]:
        """يرجع نص الرد. أزرار القوائم تغيّر self.menu[chat] وتُرجع عنوان القسم."""
        raw = (text or "").strip()
        if not raw:
            return None
        target = _BUTTON_NORM.get(_norm(raw))
        if target == "@more":
            self.menu[chat] = "more"
            return "⚙️ المزيد — اختر من الأزرار بالأسفل."
        if target == "@main":
            self.menu[chat] = "main"
            return "القائمة الرئيسية — اختر من الأزرار بالأسفل."
        if target:
            raw = target
        parts = raw.split()
        if not parts[0].startswith("/"):
            if self.free_text is not None:
                try:
                    r = self.free_text(raw, chat)
                    if r:
                        return r
                except Exception:
                    log.exception("فشل الرد على سؤال حر")
            return "اختر من الأزرار بالأسفل 👇 أو اكتب /help"
        cmd = parts[0].split("@")[0].lower()
        if cmd == "/start":
            self.menu[chat] = "main"
            return WELCOME
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
        # أزرار ReplyKeyboard ليست أوامر نصية؛ نستخرج الأمر المقابل لها أيضاً.
        button_cmd = _BUTTON_NORM.get(_norm(text))
        effective_cmd = button_cmd if button_cmd and button_cmd.startswith("/") else cmd
        if cmd == "/id":                                  # مفتوح للجميع: ليعرف المستخدم رقمه ويضعه في الإعدادات
            self.tg.send_message(chat, f"رقم حسابك (TELEGRAM_ADMIN_IDS): {uid}\nرقم هذه المحادثة: {chat}")
            return
        if self.tg.admin_ids and uid not in self.tg.admin_ids:
            return                                        # تجاهل الغرباء بصمت
        reply = self.handle_text(text, chat)
        png = None
        if effective_cmd == "/price" and self.chart_for_command is not None:
            try:
                png = self.chart_for_command()
            except Exception:
                log.exception("فشل إرسال شارت السعر")
        if reply and png:
            # السعر يظهر داخل Caption الصورة، فتصل المعلومات والشارت في رسالة واحدة.
            try:
                self.tg.send_photo(chat, png, reply, menu=self.menu_for(chat))
            except TypeError:  # توافق مع واجهات Telegram الاختبارية القديمة
                self.tg.send_photo(chat, png, reply)
        elif reply:
            self._send(chat, reply)

    def _send(self, chat, reply: str):
        try:
            self.tg.send_message(chat, reply, menu=self.menu_for(chat))
        except TypeError:                                 # واجهات اختبار قديمة بلا معامل menu
            self.tg.send_message(chat, reply)

    async def handle_update(self, u: dict):
        m = u.get("message") or {}
        if (m.get("from") or {}).get("id") is None or (m.get("chat") or {}).get("id") is None:
            return
        if not (m.get("text") or "").strip():
            return                                        # صور وملفات وغيرها: لا شيء
        await asyncio.to_thread(self.process_update, u)

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

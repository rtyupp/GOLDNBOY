"""كل نصوص الأداة بالعربي. الأكواد الداخلية (BUY / NO_SETUP ...) تبقى كما هي للمنطق، وتُترجم عند العرض فقط."""
from __future__ import annotations
import re, unicodedata

DIR = {"BUY": "شراء", "SELL": "بيع", "NO TRADE": "لا صفقة", "bull": "صاعد", "bear": "هابط"}
TREND = {"BULLISH": "صاعد", "BEARISH": "هابط", "RANGE": "عرضي", "TRANSITION": "انتقالي",
         "bull": "صاعد", "bear": "هابط", "neutral": "محايد", "conflict": "متعارض"}
STRENGTH = {"STRONG": "قوي", "WEAK": "ضعيف", "NONE": "-"}
SESSION = {"Asian": "آسيا", "London": "لندن", "New York": "نيويورك", "London/New York": "لندن/نيويورك (تداخل)",
           "Off-hours": "خارج الجلسات"}
STRATEGY = {"Trend Pullback": "ارتداد مع الاتجاه", "Liquidity Sweep": "اصطياد السيولة", "Breakout": "الاختراق",
            "Reversal": "الانعكاس", "VWAP": "فواب (VWAP)", "SMC OTE + FVG": "OTE + فجوة FVG (SMC)",
            "SMC Sweep-MSS-FVG": "سحب سيولة ثم MSS ثم FVG (SMC)",
            "Opening Range": "نطاق الافتتاح (ORB)", "Round Number": "الأرقام المستديرة",
            "Liquidity Grab (PDH/PDL)": "سحب سيولة اليوم السابق", "Silver Bullet (Killzone OTE+FVG)": "الرصاصة الفضية (نافذة + OTE + FVG)"}
BREAK = {"BOS": "كسر هيكل BOS", "CHoCH": "تغيّر طابع CHoCH"}
RESULT = {"SL": "ضرب وقف الخسارة", "TP2": "حقق الهدف الثاني", "TP1_BE": "هدف أول ثم تعادل", "EXPIRED": "انتهت المدة",
          "OPEN": "مفتوحة", "TP1": "بعد الهدف الأول", "CLOSED": "مغلقة"}
RISK_LEVEL = {"Low": "منخفضة", "Medium": "متوسطة", "High": "مرتفعة"}
SWEEP_KIND = {"sweep": "سحب سيولة", "failed_breakout": "اختراق فاشل"}
STAGE = {"DATA": "البيانات", "CANDLES": "الشموع", "INDICATORS": "المؤشرات", "STRUCTURE": "الهيكل", "LIQUIDITY": "السيولة",
         "STRATEGY": "الاستراتيجيات", "SCORE": "التقييم", "PROBABILITY": "الاحتمال التاريخي", "RISK": "المخاطرة"}
CHECKS = {
    "HTF trend": "اتجاه الفريمات الكبيرة", "pullback to EMA": "ارتداد إلى المتوسط EMA", "VWAP": "فواب VWAP",
    "structure intact": "الهيكل سليم", "confirmation": "شمعة التأكيد", "momentum": "الزخم", "zone support": "منطقة داعمة",
    "liquidity sweep": "سحب سيولة", "candle confirmation": "تأكيد الشمعة بعد السحب", "CHoCH/BOS after sweep": "CHoCH/BOS بعد السحب",
    "price still beyond sweep invalidation": "السعر لم يخترق مستوى إبطال السحب",
    "HTF not opposing": "الفريمات الكبيرة لا تعارض", "rejection / stop-hunt": "رفض سعري / صيد وقف", "quality pool": "مستوى سيولة قوي",
    "range / compression": "نطاق ضيق (ضغط)", "breakout close": "إغلاق خارج النطاق", "volume (tick) expansion": "توسع النشاط (Tick)",
    "retest holds": "إعادة اختبار ناجحة", "extreme": "تطرف سعري", "liquidity": "سيولة", "divergence": "دايفرجنس",
    "structure shift (CHoCH/BOS)": "تحول الهيكل (CHoCH/BOS)", "not against strong HTF": "ليس ضد اتجاه قوي أعلى",
    "trend": "الاتجاه", "VWAP retest": "إعادة اختبار VWAP", "15m side of EMA50": "جهة EMA50 على 15 دقيقة",
    # ---- SMC (goldbot/strategies/smc.py)
    "BOS/CHoCH in direction": "كسر هيكل BOS/CHoCH في الاتجاه", "impulse leg": "ساق الحركة الأساسية",
    "price in OTE 62-79%": "السعر داخل منطقة OTE (62%-79%)", "confirmation candle": "شمعة تأكيد في الاتجاه",
    "FVG overlaps OTE": "فجوة FVG تتقاطع مع OTE", "strict order block at OTE": "Order Block صارم داخل OTE",
    "liquidity sweep (SMC)": "سحب سيولة قبل الدخول", "momentum (SMC)": "زخم RSI/MACD في الاتجاه",
    "sweep confirmed": "سحب سيولة مؤكد", "MSS after sweep": "تغيّر هيكل MSS بعد السحب",
    "FVG created by MSS": "فجوة FVG خلقها كسر الهيكل", "price inside FVG": "السعر داخل الفجوة FVG",
    "pool quality": "مستوى سيولة قوي (جلسة/يومي)", "session active": "جلسة نشطة",
    # ---- ORB + Round Number (goldbot/strategies/impl.py)
    "opening range": "نطاق الافتتاح", "break of opening range": "إغلاق خارج نطاق الافتتاح",
    "liquid session": "جلسة ذات سيولة", "not overextended": "غير ممتد بعيدًا عن المستوى",
    "volume expansion": "توسّع النشاط", "near round level": "قريب من رقم مستدير",
    "touched level": "ملامسة المستوى", "rejection candle": "شمعة رفض", "VWAP side": "جهة VWAP",
    # ---- Liquidity Grab + Silver Bullet (goldbot/strategies/advanced.py)
    "daily level sweep (PDH/PDL)": "سحب قمة/قاع اليوم السابق", "level reclaimed": "استرجاع المستوى",
    "killzone window": "داخل نافذة ICT الزمنية", "displacement (ATR)": "حركة قوية (ATR)",
}
DETAIL_REPL = [("wr=", "نسبة الربح "), ("avgR=", "متوسط R "), ("n=", "العينة "), ("ER=", "كفاءة الاتجاه "),
               ("RR2=", "RR2 "), ("min", "د"), ("ATR", "ATR")]
REASONS = {
    "NO_TICK_YET": "لم يصل أي سعر لحظي بعد", "WS_DISCONNECTED": "الاتصال اللحظي (WebSocket) منقطع",
    "MARKET_CLOSED": "السوق مغلق (عطلة نهاية الأسبوع) — يستأنف التحليل تلقائيًا عند الافتتاح", "STALE_DATA": "بيانات السعر قديمة", "NO_QUOTE": "لا يوجد سعر Bid/Ask", "NO_CANDLES": "لا توجد شموع كافية",
    "CANDLE_GAP": "فجوة في الشموع", "INSUFFICIENT_HISTORY": "بيانات تاريخية غير كافية", "NO_ANALYSIS": "لا يوجد تحليل",
    "SPREAD_HIGH": "السبريد مرتفع", "SPREAD_VS_RISK": "السبريد كبير مقارنة بالمخاطرة", "HTF_CONFLICT": "تعارض اتجاه الفريمات الكبيرة (4H مقابل 1H)",
    "CHOPPY_MARKET": "سوق متذبذب بلا اتجاه واضح", "OFF_HOURS": "خارج جلسات التداول", "NO_SETUP": "لا يوجد إعداد مكتمل",
    "LOW_CONFLUENCE": "تقاطع الأدلة ضعيف", "WEAK_PROBABILITY": "الاحتمال التاريخي ضعيف",
    "PROBABILITY_UNAVAILABLE": "الاحتمال التاريخي غير متوفر (عينة غير كافية)",
    "CONFLICTING_STRATEGIES": "استراتيجيات متعارضة (شراء مقابل بيع)",
    "REGIME_BLOCK": "حالة السوق (1H) مستبعدة بحسب الإعداد",
    "DAILY_LOSS_LIMIT": "بلغ حد الخسارة اليومي — إيقاف الإشارات الجديدة لبقية اليوم",
    "LOSS_STREAK": "سلسلة خسائر متتالية — إيقاف مؤقت للإشارات",
    "OPEN_TRADE_EXISTS": "توجد صفقة مفتوحة", "COOLDOWN": "فترة انتظار بعد آخر إشارة",
    "TELEGRAM_SEND_FAILED": "فشل الإرسال إلى تيليجرام", "SL_ILLOGICAL": "وقف الخسارة غير منطقي",
    "SL_TOO_WIDE": "وقف الخسارة واسع جدًا", "RR_POOR": "نسبة العائد للمخاطرة ضعيفة", "RR_POOR_TARGET": "الهدف قريب من سيولة معاكسة",
    "NEWS_BLACKOUT": "تنبيه خبر اقتصادي مؤثر — نوقف الإشارات حول وقت الخبر",
}
_R = re.compile(r"^([A-Z_]+)\((.*)\)$")


def reason_ar(code: str) -> str:
    m = _R.match(code)
    name, d = (m.group(1), m.group(2)) if m else (code, "")
    base = REASONS.get(name)
    if base is None:
        return code
    for a, b in DETAIL_REPL:
        d = d.replace(a, b)
    return f"{base} ({d})" if d else base


def reasons_ar(codes) -> str:
    return " | ".join(reason_ar(c) for c in codes)


def check_ar(name: str) -> str:
    return CHECKS.get(name, name)


def strategy_reason_ar(text: str) -> str:
    """يترجم نص سبب الاستراتيجية (القائم على أسماء الفحوص) للعرض."""
    if not text:
        return ""
    if text.startswith("missing: "):
        return "ينقص: " + "، ".join(check_ar(x.strip()) for x in text[9:].split(","))
    if text.startswith("score "):
        return "النتيجة " + text[6:].replace(" < ", " أقل من ")
    if text.startswith("risk rejected: "):
        return "رفض المخاطرة: " + reason_ar(text[15:])
    if text == "no invalidation level":
        return "لا يوجد مستوى إبطال"
    return " + ".join(check_ar(x.strip()) for x in text.split(" + "))


def final_ar(final: str) -> str:
    if final == "NO TRADE":
        return "لا صفقة"
    if final.endswith(" SENT"):
        return f"تم إرسال إشارة {DIR.get(final.split()[0], final)}"
    return final


# ------------------------------------------------------------------ Arabic shaping for matplotlib (no extra deps)
_FORMS: dict = {}
for _cp in range(0xFE70, 0xFEFD):
    _ch = chr(_cp)
    _dec = unicodedata.decomposition(_ch)
    if not _dec.startswith("<"):
        continue
    _tag, *_rest = _dec.split()
    _tag = _tag.strip("<>")
    if _tag in ("isolated", "final", "initial", "medial") and "0020" not in _rest:
        _FORMS.setdefault("".join(chr(int(x, 16)) for x in _rest), {})[_tag] = _ch
_ALEF = set("\u0622\u0623\u0625\u0627")
_ARABIC = re.compile("[\u0600-\u06FF]")


def _dual(c):
    return "initial" in _FORMS.get(c, {})


def _joins_next(c):
    return _dual(c)


def shape_word(w: str) -> str:
    out, i, n = [], 0, len(w)
    prev_joins = False
    while i < n:
        c = w[i]
        if c == "\u0644" and i + 1 < n and w[i + 1] in _ALEF and (c + w[i + 1]) in _FORMS:
            f = _FORMS[c + w[i + 1]]
            out.append(f.get("final") if prev_joins and "final" in f else f.get("isolated", c))
            prev_joins = False
            i += 2
            continue
        f = _FORMS.get(c)
        if not f:
            out.append(c)
            prev_joins = False
            i += 1
            continue
        nxt = w[i + 1] if i + 1 < n else ""
        next_joins = bool(nxt) and nxt in _FORMS and "final" in _FORMS[nxt] and _dual(c)
        if prev_joins and next_joins and "medial" in f:
            g = f["medial"]
        elif prev_joins and "final" in f:
            g = f["final"]
        elif next_joins and "initial" in f:
            g = f["initial"]
        else:
            g = f.get("isolated", c)
        out.append(g)
        prev_joins = _dual(c)
        i += 1
    return "".join(out)


def ar(text: str) -> str:
    """نص عربي جاهز للرسم داخل matplotlib (تشكيل الحروف + اتجاه من اليمين لليسار)."""
    try:                                   # الأفضل إن كانت المكتبتان مثبتتين
        import arabic_reshaper
        from bidi.algorithm import get_display
        return get_display(arabic_reshaper.reshape(text))
    except Exception:
        pass
    toks = text.split(" ")
    toks = [shape_word(t)[::-1] if _ARABIC.search(t) else t for t in toks]
    return " ".join(reversed(toks))

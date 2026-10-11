"""وعي البوت ببيئته: حد الذاكرة المتاح (Render المجاني ≈ 512MB) وإغلاق سوق الذهب الأسبوعي.
كل الدوال آمنة: إن تعذّر القياس ترجع None ولا تُسقط البوت."""
from __future__ import annotations
import os
from typing import Optional
import pandas as pd

_CG2 = ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory.current")
_CG1 = ("/sys/fs/cgroup/memory/memory.limit_in_bytes", "/sys/fs/cgroup/memory/memory.usage_in_bytes")


def _read_int(path: str) -> Optional[int]:
    try:
        with open(path) as f:
            v = f.read().strip()
        return None if v in ("max", "") else int(v)
    except (OSError, ValueError):
        return None


def limit_mb() -> Optional[float]:
    """حد ذاكرة الحاوية. MEMORY_LIMIT_MB يتجاوز القياس التلقائي. القيم الضخمة (بلا حد) = None."""
    env = os.environ.get("MEMORY_LIMIT_MB")
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    for lim, _ in (_CG2, _CG1):
        b = _read_int(lim)
        if b and b < (1 << 50):
            return b / 1048576.0
    return None


def usage_mb() -> Optional[float]:
    """ذاكرة الحاوية كلها (البوت + أي عملية فرعية). إن غاب cgroup نستعمل RSS للعملية الحالية."""
    for _, cur in (_CG2, _CG1):
        b = _read_int(cur)
        if b:
            return b / 1048576.0
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except (OSError, ValueError, IndexError):
        pass
    return None


def pressure() -> Optional[float]:
    """نسبة الاستهلاك من الحد (0..1+). None إن لم يُعرف الحد."""
    lim, use = limit_mb(), usage_mb()
    return None if not lim or use is None else use / lim


def market_closed(ts: pd.Timestamp) -> bool:
    """إغلاق الذهب الأسبوعي: من الجمعة 21:00 UTC إلى الأحد 22:00 UTC (تقريب متحفظ يشمل فرق التوقيت الصيفي)."""
    ts = ts.tz_convert("UTC") if ts.tzinfo else ts.tz_localize("UTC")
    wd, h = ts.weekday(), ts.hour + ts.minute / 60.0
    return (wd == 4 and h >= 21.0) or wd == 5 or (wd == 6 and h < 22.0)


def plan_bootstrap(cfg) -> dict:
    """يضبط حجم بناء السجل التاريخي حسب الذاكرة: كلما قلّت الذاكرة قلّت الأيام والفريمات."""
    days = int(cfg.get("bootstrap.days", 45))
    tfs = list(cfg.get("bootstrap.tfs", ["5m", "15m"]))
    lim = limit_mb()
    if lim is None or not cfg.get("bootstrap.adaptive", True):
        return {"run": True, "days": days, "tfs": tfs, "limit_mb": lim, "why": "بلا قيد ذاكرة معروف"}
    if lim < 400:
        return {"run": False, "days": 0, "tfs": [], "limit_mb": lim, "why": f"الذاكرة {lim:.0f}MB لا تكفي للتهيئة"}
    if lim < 1000:                                    # الخطة المجانية (512MB)
        days, tfs = min(days, int(cfg.get("bootstrap.low_mem_days", 7))), ["15m"]
    elif lim < 2000:
        days, tfs = min(days, 21), tfs[:2]
    use = usage_mb()
    if use is not None and lim - use < float(cfg.get("bootstrap.min_free_mb", 150)):
        return {"run": False, "days": days, "tfs": tfs, "limit_mb": lim,
                "why": f"المتاح {lim - use:.0f}MB فقط الآن"}
    return {"run": True, "days": days, "tfs": tfs, "limit_mb": lim, "why": f"ضُبط حسب الذاكرة ({lim:.0f}MB)"}

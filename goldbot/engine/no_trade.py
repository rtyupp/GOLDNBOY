"""NO TRADE is a valid result. Every gate that can veto a trade lives here (with a clear reason).

ملاحظة: لا توجد بوابة أخبار ولا بوابة ذكاء اصطناعي. غياب أي مصدر خارجي لا يمنع التحليل ولا الإشارات.
"""
from __future__ import annotations
from typing import List


def global_gates(cfg, ctx, data_ok: bool, data_reason: str) -> List[str]:
    """Gates that do not depend on a specific setup."""
    r: List[str] = []
    if not data_ok:
        r.append(data_reason)
        return r
    if ctx is None:
        return ["NO_ANALYSIS"]
    if ctx.spread is None:
        r.append("NO_QUOTE")
    elif ctx.spread > cfg.get("risk.max_spread", 0.8):
        r.append(f"SPREAD_HIGH({ctx.spread:.2f})")
    if ctx.mtf["bias"] == "conflict" and not cfg.get("confluence.ignore_htf_conflict", False):
        r.append("HTF_CONFLICT(4H vs 1H)")
    s15 = ctx.tf["15m"]
    er = float(s15.last["er"])
    if er < cfg.get("filters.min_efficiency_ratio", 0.12) and s15.trend in ("RANGE", "TRANSITION") and ctx.tf["1H"].trend == "RANGE":
        r.append(f"CHOPPY_MARKET(ER={er:.2f})")
    # حالة السوق (اتجاه 1H): أظهرت الاختبارات أن أنظمة «الانتقالي» أسوأ حالًا بفارق واضح.
    # قابلة للتعطيل من الإعداد، ولا تُحجب إلا ما يحدده المستخدم صراحة.
    for bad in (cfg.get("filters.block_regimes", []) or []):
        if ctx.regime == bad:
            r.append(f"REGIME_BLOCK({bad})")
    if ctx.session["label"] == "Off-hours" and not cfg.get("filters.allow_off_hours", False):
        r.append("OFF_HOURS")
    return r


def adaptive_min_score(cfg, journal) -> float:
    """عتبة التقاطع التكيّفية: عند خسارة الأداء الأخير (متوسط R لآخر n صفقة < 0) نرفع العتبة قليلًا
    بدل إيقاف التداول كليًا، فتصمد فقط الإعدادات عالية الجودة. معطّلة افتراضيًا (0 = لا تعديل)."""
    step = float(cfg.get("confluence.adaptive_step", 0) or 0)
    base = float(cfg.get("confluence.min_score", 70))
    if step <= 0 or journal is None:
        return base
    n = int(cfg.get("confluence.adaptive_lookback", 20) or 20)
    min_n = int(cfg.get("confluence.adaptive_min_samples", 10) or 10)
    avg, count = journal.recent_avg_r(n)
    if count < min_n or avg >= 0:
        return base
    bump = min(step, float(cfg.get("confluence.adaptive_max_bump", step) or step))
    return base + bump


def setup_gates(cfg, best, confluence_score: float, prob, journal=None) -> List[str]:
    r: List[str] = []
    if best is None or best.signal == "NONE":
        r.append("NO_SETUP")
        return r
    min_score = adaptive_min_score(cfg, journal)
    if confluence_score < min_score:
        r.append(f"LOW_CONFLUENCE({confluence_score:.0f}<{min_score:.1f})")
    if prob is not None:
        if prob.sufficient:
            if prob.win_rate < cfg.get("probability.min_win_rate", 0.50) or prob.avg_r < cfg.get("probability.min_avg_r", 0.0):
                r.append(f"WEAK_PROBABILITY(wr={prob.win_rate:.0%},avgR={prob.avg_r:.2f},n={prob.n})")
        elif cfg.get("probability.block_if_insufficient", True):
            r.append(f"PROBABILITY_UNAVAILABLE(n={prob.n}<{cfg.get('probability.min_samples', 30)})")
    return r


def capital_gates(cfg, journal, as_of) -> List[str]:
    """حماية رأس المال: توقف عن إصدار إشارات جديدة بعد خسارة يومية أو سلسلة خسائر (اختياري، 0 = معطّل).
    لا يعتمد على ctx: تُقيَّد فقط بعد أن يكون هناك إعداد قابل للتداول حتى لا تُلوَّن الدورات الفارغة."""
    if journal is None:
        return []
    r: List[str] = []
    max_daily = float(cfg.get("signals.max_daily_loss_r", 0) or 0)
    if max_daily > 0:
        start = as_of.normalize() if as_of.tzinfo else as_of.tz_localize("UTC").normalize()
        day_r = journal.realized_r_since(start)
        if day_r <= -max_daily:
            r.append(f"DAILY_LOSS_LIMIT({day_r:+.2f}R<={-max_daily:.2f}R)")
    max_streak = int(cfg.get("signals.max_consecutive_losses", 0) or 0)
    if max_streak > 0:
        streak = journal.consecutive_losses()
        if streak >= max_streak:
            r.append(f"LOSS_STREAK({streak}>={max_streak})")
    return r

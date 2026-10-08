"""NO TRADE is a valid result. Every gate that can veto a trade lives here (with a clear reason)."""
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
    if not getattr(ctx.news, "calendar_ok", True):
        r.append("NEWS_CALENDAR_UNAVAILABLE")
    if ctx.news.state == "PRE_NEWS":
        r.append(f"NEWS_HIGH_RISK({ctx.news.event}|{ctx.news.minutes:.0f})")
    elif ctx.news.state == "POST_NEWS":
        r.append(f"NEWS_SETTLING({ctx.news.event}|{ctx.news.minutes:.0f})")
    if ctx.spread is None:
        r.append("NO_QUOTE")
    elif ctx.spread > cfg.get("risk.max_spread", 0.8):
        r.append(f"SPREAD_HIGH({ctx.spread:.2f})")
    if ctx.mtf["bias"] == "conflict":
        r.append("HTF_CONFLICT(4H vs 1H)")
    s15 = ctx.tf["15m"]
    er = float(s15.last["er"])
    if er < cfg.get("filters.min_efficiency_ratio", 0.12) and s15.trend in ("RANGE", "TRANSITION") and ctx.tf["1H"].trend == "RANGE":
        r.append(f"CHOPPY_MARKET(ER={er:.2f})")
    if ctx.session["label"] == "Off-hours" and not cfg.get("filters.allow_off_hours", False):
        r.append("OFF_HOURS")
    return r


def setup_gates(cfg, best, confluence_score: float, prob, ai_decision=None) -> List[str]:
    r: List[str] = []
    if best is None or best.signal == "NONE":
        r.append("NO_SETUP")
        return r
    if confluence_score < cfg.get("confluence.min_score", 70):
        r.append(f"LOW_CONFLUENCE({confluence_score:.0f}<{cfg.get('confluence.min_score', 70)})")
    if prob is not None:
        if prob.sufficient:
            if prob.win_rate < cfg.get("probability.min_win_rate", 0.50) or prob.avg_r < cfg.get("probability.min_avg_r", 0.0):
                r.append(f"WEAK_PROBABILITY(wr={prob.win_rate:.0%},avgR={prob.avg_r:.2f},n={prob.n})")
        elif cfg.get("probability.block_if_insufficient", True):
            r.append(f"PROBABILITY_UNAVAILABLE(n={prob.n}<{cfg.get('probability.min_samples', 30)})")
    if ai_decision is not None and ai_decision.decision != best.signal:
        r.append(f"AI_REJECTED({ai_decision.decision}: {ai_decision.reason[:80]})")
    return r

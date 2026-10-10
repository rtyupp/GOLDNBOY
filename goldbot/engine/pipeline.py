"""LIVE DATA -> candles -> indicators -> structure -> liquidity -> strategies -> confluence ->
probability -> risk -> BUY / SELL / NO TRADE.  Same code path for live and backtest."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional
import pandas as pd

from goldbot.analysis.analyzer import Analyzer, Ctx
from goldbot.engine import no_trade
from goldbot.engine.confluence import confluence
from goldbot.engine.probability import ProbabilityEngine, Probability, fingerprint
from goldbot.engine.risk import RiskEngine
from goldbot.models import StrategyResult
from goldbot.monitor import CycleLog
from goldbot.strategies.impl import ALL
from goldbot import i18n as T
from goldbot.ml.model import features as ml_features


@dataclass
class Candidate:
    ctx: Optional[Ctx]
    log: CycleLog
    results: List[StrategyResult] = field(default_factory=list)
    best: Optional[StrategyResult] = None
    confluence: float = 0.0
    conf_parts: Dict[str, float] = field(default_factory=dict)
    prob: Optional[Probability] = None
    risk_level: str = "-"
    fp: dict = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)      # gate reasons so far (empty => ready for AI)
    global_reasons: List[str] = field(default_factory=list)  # setup-independent gates (used by backtest)

    @property
    def tradable(self) -> bool:
        return self.best is not None and self.best.signal != "NONE" and not self.reasons


class Pipeline:
    def __init__(self, cfg, journal=None, ml=None, only: Optional[List[str]] = None):
        self.cfg = cfg
        self.analyzer = Analyzer(cfg)
        self.risk = RiskEngine(cfg)
        self.ml = ml
        self.prob_engine = ProbabilityEngine(journal, cfg) if journal is not None else None
        keys = only or cfg.get("strategies.enabled", [c.key for c in ALL])
        self.strategies = [c(cfg, self.risk) for c in ALL if c.key in keys]

    # ---- stage 1..: all local stages -------------------------------------------------
    def prepare(self, frames: Dict[str, pd.DataFrame], bid: float, ask: float, as_of: pd.Timestamp,
                data_ok: bool = True, data_reason: str = "OK", use_probability: bool = True) -> Candidate:
        lg = CycleLog(ts=as_of.strftime("%Y-%m-%d %H:%M UTC"))
        lg.set("DATA", "سليمة" if data_ok else f"فشل ({T.reason_ar(data_reason)})")
        cand = Candidate(None, lg)
        if not data_ok:
            lg.set("CANDLES", "تم التخطي")
            cand.reasons = no_trade.global_gates(self.cfg, None, False, data_reason)
            return self._done(cand)
        ctx, why = self.analyzer.analyze(frames, bid, ask, as_of)
        if ctx is None:
            lg.set("CANDLES", f"فشل ({T.reason_ar(why)})")
            cand.reasons = [why]
            return self._done(cand)
        cand.ctx = ctx
        lg.set("CANDLES", "سليمة  " + " ".join(f"{tf}:{len(frames[tf])}" for tf in ("1m", "5m", "15m", "1H", "4H")))
        lg.set("INDICATORS", "سليمة")
        t = ctx.mtf["labels"]
        lg.set("STRUCTURE", " ".join(f"{tf}={T.TREND[t[tf]]}" for tf in ("4H", "1H", "15m", "5m", "1m")) + f" | الاتجاه العام={T.TREND.get(ctx.mtf['bias'], ctx.mtf['bias'])}/{T.STRENGTH[ctx.mtf['strength']]}")
        sw = [f"{T.SWEEP_KIND.get(s.kind, s.kind)} {'للأعلى' if s.expected == 'bull' else 'للأسفل'} ({s.pool_kind}){' مؤكد' if s.confirmed else ''}" for s in (ctx.tf['5m'].sweeps + ctx.tf['15m'].sweeps)[:3]]
        lg.set("LIQUIDITY", "، ".join(sw) if sw else "لا يوجد")
        lg.extra["session"] = ctx.session["label"]

        reasons = no_trade.global_gates(self.cfg, ctx, True, "OK")
        cand.global_reasons = list(reasons)
        # ---- strategies
        cand.results = [s.evaluate(ctx) for s in self.strategies]
        valid = [r for r in cand.results if r.signal != "NONE"]
        lg.set("STRATEGY", " ؛ ".join(f"{T.STRATEGY.get(r.name, r.name)}: " + (f"{T.DIR[r.signal]} ({r.score:.0f})" if r.signal != "NONE" else f"لا إشارة ({r.score:.0f}) [{T.strategy_reason_ar(r.reason)[:60]}]") for r in cand.results))
        if not valid:
            lg.set("SCORE", "-")
            cand.reasons = reasons + ["NO_SETUP"]
            return self._done(cand)
        # confluence per direction, pick best combined
        conf_cache = {}
        for r in valid:
            d = r.setup_tags["direction"]
            if d not in conf_cache:
                conf_cache[d] = confluence(ctx, d)
        scored = []
        for r in valid:
            cs, parts = conf_cache[r.setup_tags["direction"]]
            scored.append((0.4 * r.score + 0.6 * cs, r, cs, parts))
        scored.sort(key=lambda x: -x[0])
        _, best, cs, parts = scored[0]
        buys = [s for s in scored if s[1].signal == "BUY"]
        sells = [s for s in scored if s[1].signal == "SELL"]
        if buys and sells and abs(buys[0][0] - sells[0][0]) < self.cfg.get("confluence.conflict_margin", 10):
            reasons.append("CONFLICTING_STRATEGIES(BUY vs SELL)")
        cand.best, cand.confluence, cand.conf_parts = best, cs, parts
        agree = sum(1 for r in valid if r.signal == best.signal)
        lg.set("SCORE", f"{cs:.0f}/100 ({T.STRATEGY.get(best.name, best.name)}، موافقة {agree}/{len(self.strategies)} استراتيجيات)")
        # ---- probability (historical, never invented)
        cand.fp = fingerprint(ctx, best)
        try:                                   # مؤشرات لحظة الإشارة: تُخزَّن لتدريب التعلم الآلي لاحقًا من صفقاتك الحية أيضًا
            cand.fp["features"] = ml_features(ctx, best, cs)
        except Exception:
            pass
        if use_probability and self.prob_engine is not None:
            cand.prob = self.prob_engine.estimate(cand.fp, as_of)
            lg.set("PROBABILITY", cand.prob.text())
        else:
            lg.set("PROBABILITY", "غير مستخدم")
        cand.risk_level = self.risk.risk_level(ctx, best.plan)
        p = best.plan
        lg.set("RISK", f"{T.RISK_LEVEL[cand.risk_level]}  دخول={p.entry} وقف={p.sl} هدف1={p.tp1} هدف2={p.tp2} RR={p.rr1}/{p.rr2}")
        reasons += no_trade.setup_gates(self.cfg, best, cs, cand.prob if use_probability else None)
        if self.ml is not None and self.ml.active:
            ml_p = self.ml.predict_win_prob(ctx, best, cs)
            lg.extra["ml_prob"] = ml_p
            if ml_p is not None and ml_p < self.cfg.get("ml.min_prob", 0.5):
                reasons.append(f"ML_LOW({ml_p:.2f})")
        cand.reasons = reasons
        return self._done(cand)

    def _done(self, cand: Candidate) -> Candidate:
        if cand.reasons:
            cand.log.final = "NO TRADE"
            cand.log.reasons = cand.reasons
        return cand

    # ---- finalize local decision ---------------------------------------------------------------
    def finalize(self, cand: Candidate) -> Candidate:
        """اعتماد الإشارة بعد اجتياز جميع الفلاتر المحلية."""
        if not cand.tradable:
            return cand
        cand.log.set("DECISION", "اعتماد محلي من الاستراتيجيات والمؤشرات")
        cand.log.final = f"{cand.best.signal} SENT"
        return cand

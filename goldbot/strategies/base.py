from __future__ import annotations
from typing import List, Optional, Tuple
from goldbot.models import StrategyResult
from goldbot.engine.risk import RiskEngine


def opp(d: str) -> str:
    return "bear" if d == "bull" else "bull"


class Checks:
    def __init__(self):
        self.items: List[Tuple[str, bool, float, bool]] = []

    def add(self, name: str, ok: bool, weight: float, required: bool = False):
        self.items.append((name, bool(ok), weight, required))

    @property
    def score(self) -> float:
        tot = sum(w for _, _, w, _ in self.items) or 1
        return 100.0 * sum(w for _, ok, w, _ in self.items if ok) / tot

    @property
    def required_ok(self) -> bool:
        return all(ok for _, ok, _, req in self.items if req)

    def failed_required(self) -> List[str]:
        return [n for n, ok, _, req in self.items if req and not ok]

    def passed(self) -> List[str]:
        return [n for n, ok, _, _ in self.items if ok]

    def as_dict(self):
        return {n: ok for n, ok, _, _ in self.items}


class Strategy:
    name = "BASE"
    label = "Base"

    def __init__(self, cfg, risk: RiskEngine):
        self.cfg = cfg
        self.risk = risk
        self.min_score = cfg.get(f"strategies.{self.key}.min_score", cfg.get("strategies.min_score", 60))

    key = "base"

    def check(self, ctx, d: str) -> Tuple[Checks, Optional[float], dict]:
        """Return (checks, invalidation_price, tags)."""
        raise NotImplementedError

    def evaluate(self, ctx) -> StrategyResult:
        best: Optional[StrategyResult] = None
        for d in ("bull", "bear"):
            ch, inval, tags = self.check(ctx, d)
            res = StrategyResult(self.label, "NONE", round(ch.score, 1), checks=ch.as_dict(), setup_tags=tags,
                                 invalidation=inval)
            sig = "BUY" if d == "bull" else "SELL"
            if not ch.required_ok:
                res.reason = "missing: " + ", ".join(ch.failed_required())
            elif ch.score < self.min_score:
                res.reason = f"score {ch.score:.0f} < {self.min_score}"
            elif inval is None:
                res.reason = "no invalidation level"
            else:
                plan, why = self.risk.build(ctx, d, inval)
                if plan is None:
                    res.reason = f"risk rejected: {why}"
                else:
                    res.signal, res.plan = sig, plan
                    res.entry, res.sl, res.tp1, res.tp2 = plan.entry, plan.sl, plan.tp1, plan.tp2
                    res.reason = " + ".join(ch.passed())
            res.setup_tags = dict(tags, direction=d)
            if best is None or (res.signal != "NONE" and best.signal == "NONE") or \
               ((res.signal != "NONE") == (best.signal != "NONE") and res.score > best.score):
                best = res
        return best

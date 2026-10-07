from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, List, Any


@dataclass
class Tick:
    ts_ms: int                      # source timestamp (epoch ms)
    bid: Optional[float]
    ask: Optional[float]
    last: Optional[float]
    recv_ms: int                    # local receive time (epoch ms)

    @property
    def has_quote(self) -> bool:
        return self.bid is not None and self.ask is not None and self.ask >= self.bid > 0

    @property
    def mid(self) -> float:
        if self.has_quote:
            return (self.bid + self.ask) / 2.0
        return float(self.last)

    @property
    def spread(self) -> Optional[float]:
        return (self.ask - self.bid) if self.has_quote else None


@dataclass
class RiskPlan:
    direction: str
    entry: float
    sl: float
    tp1: float
    tp2: float
    risk: float
    reward1: float
    reward2: float
    rr1: float
    rr2: float
    notes: List[str] = field(default_factory=list)


@dataclass
class StrategyResult:
    name: str
    signal: str = "NONE"            # BUY / SELL / NONE
    score: float = 0.0              # 0..100
    entry: Optional[float] = None
    sl: Optional[float] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    reason: str = ""
    checks: Dict[str, bool] = field(default_factory=dict)
    plan: Optional[RiskPlan] = None
    invalidation: Optional[float] = None
    setup_tags: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)

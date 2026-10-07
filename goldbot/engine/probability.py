"""Historical Probability Engine. Score != probability: a win-rate is shown only when computed from
real historical trades (backtest + live journal) with a sufficient sample size."""
from __future__ import annotations
import math
from dataclasses import dataclass
from typing import Optional
import pandas as pd


@dataclass
class Probability:
    sufficient: bool
    n: int
    wins: int
    losses: int
    win_rate: float
    avg_r: float
    ci_low: float
    ci_high: float
    level: str                    # which similarity level matched
    note: str = ""

    def text(self) -> str:
        if not self.sufficient:
            return f"غير كافٍ (عدد الصفقات المشابهة {self.n})"
        return f"{self.win_rate:.1%} (عدد الصفقات المشابهة {self.n}، متوسط R {self.avg_r:.2f})"


def wilson(wins: int, n: int, z: float = 1.96):
    if n == 0:
        return 0.0, 0.0
    p = wins / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, centre - half), min(1.0, centre + half)


def atr_bucket(atr_pct: float) -> str:
    return "low" if atr_pct < 0.33 else ("mid" if atr_pct < 0.66 else "high")


def rsi_bucket(rsi: float) -> str:
    return "<35" if rsi < 35 else ("35-50" if rsi < 50 else ("50-65" if rsi < 65 else ">65"))


def fingerprint(ctx, res) -> dict:
    s5 = ctx.tf["5m"]
    atr_s = s5.df["atr"].dropna().iloc[-300:]
    pct = float((atr_s < s5.atr).mean()) if len(atr_s) > 20 else 0.5
    d = res.setup_tags.get("direction", "bull" if res.signal == "BUY" else "bear")
    sweep = any(s.expected == d and s.bars_ago <= 12 for s in s5.sweeps + ctx.tf["15m"].sweeps)
    lb = s5.last_break
    return {
        "setup": res.name, "direction": res.signal,
        "session": ctx.session["label"], "htf_bias": ctx.mtf["bias"],
        "structure": f"{s5.trend}/{lb.kind if lb else '-'}",
        "atr_bucket": atr_bucket(pct), "rsi_bucket": rsi_bucket(float(s5.last["rsi"])),
        "liquidity": "sweep" if sweep else "none", "regime": ctx.regime,
    }


# progressively relaxed similarity levels
LEVELS = [
    ("L1 setup+dir+session+bias+atr+rsi+structure+liq", ["setup", "direction", "session", "htf_bias", "atr_bucket", "rsi_bucket", "structure", "liquidity"]),
    ("L2 setup+dir+session+bias+atr+liq", ["setup", "direction", "session", "htf_bias", "atr_bucket", "liquidity"]),
    ("L3 setup+dir+session+bias", ["setup", "direction", "session", "htf_bias"]),
    ("L4 setup+dir+bias", ["setup", "direction", "htf_bias"]),
]


class ProbabilityEngine:
    def __init__(self, journal, cfg):
        self.journal = journal
        self.min_samples = int(cfg.get("probability.min_samples", 30))
        self.exclude_after: Optional[str] = None

    def estimate(self, fp: dict, as_of: Optional[pd.Timestamp] = None) -> Probability:
        trades = self.journal.history_trades(before=as_of)
        best_n, best_lvl = 0, "none"
        for name, keys in LEVELS:
            df = trades
            if df.empty:
                break
            for k in keys:
                df = df[df[k] == fp.get(k)]
            n = len(df)
            if n > best_n:
                best_n, best_lvl = n, name
            if n >= self.min_samples:
                wins = int((df["r"] > 0).sum())
                lo, hi = wilson(wins, n)
                return Probability(True, n, wins, n - wins, wins / n, float(df["r"].mean()), lo, hi, name)
        return Probability(False, best_n, 0, 0, 0.0, 0.0, 0.0, 0.0, best_lvl, "not enough similar historical setups")

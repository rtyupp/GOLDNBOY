from __future__ import annotations
from typing import Dict
import pandas as pd


def htf_levels(daily: pd.DataFrame) -> Dict[str, float]:
    """PDH/PDL = last closed UTC day, PWH/PWL = last completed calendar week (Mon-Sun)."""
    out: Dict[str, float] = {}
    if daily.empty:
        return out
    out["PDH"], out["PDL"] = float(daily["high"].iloc[-1]), float(daily["low"].iloc[-1])
    wk = daily.index.tz_localize(None).to_period("W-SUN")
    cur = wk[-1]
    prev = daily[wk < cur]
    if len(prev):
        pw = wk[wk < cur][-1]
        seg = prev[wk[wk < cur] == pw]
        out["PWH"], out["PWL"] = float(seg["high"].max()), float(seg["low"].min())
    return out

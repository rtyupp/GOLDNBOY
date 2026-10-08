"""Gold session engine (UTC windows editable in config; DST is NOT auto-adjusted)."""
from __future__ import annotations
from typing import Dict, List, Optional
import pandas as pd

DEFAULT_SESSIONS = {"asia": [0, 8], "london": [7, 16], "new_york": [12, 21]}
LABELS = {"asia": "Asian", "london": "London", "new_york": "New York"}


def active_sessions(ts: pd.Timestamp, sessions: dict) -> List[str]:
    h = ts.hour + ts.minute / 60.0
    return [k for k, (a, b) in sessions.items() if a <= h < b]


def session_label(ts: pd.Timestamp, sessions: dict) -> str:
    act = active_sessions(ts, sessions)
    if "london" in act and "new_york" in act:
        return "London/New York"
    if act:
        return LABELS[act[0]]
    return "Off-hours"


def _window(ts: pd.Timestamp, a: float, b: float, days_back: int) -> tuple:
    day = ts.floor("1D") - pd.Timedelta(days=days_back)
    return day + pd.Timedelta(hours=a), day + pd.Timedelta(hours=b)


def completed_session_levels(df15: pd.DataFrame, as_of: pd.Timestamp, sessions: dict) -> Dict[str, float]:
    """High/Low of the most recent COMPLETED window of each session (usable as liquidity pools)."""
    out: Dict[str, float] = {}
    if df15.empty:
        return out
    for name, (a, b) in sessions.items():
        for back in (0, 1, 2, 3, 4):
            s, e = _window(as_of, a, b, back)
            if e <= as_of:
                seg = df15[(df15.index >= s) & (df15.index < e)]
                if len(seg):
                    code = {"asia": "ASIA", "london": "LONDON", "new_york": "NY"}[name]
                    out[f"{code}_H"] = float(seg["high"].max())
                    out[f"{code}_L"] = float(seg["low"].min())
                    break
    return out


def session_info(df5: pd.DataFrame, as_of: pd.Timestamp, sessions: dict, or_minutes: int = 30,
                 completed: Optional[Dict[str, float]] = None) -> dict:
    label = session_label(as_of, sessions)
    act = active_sessions(as_of, sessions)
    info = {"label": label, "active": act, "overlap": ("london" in act and "new_york" in act),
            "high": None, "low": None, "range": None, "opening_range": None, "breakout": None}
    if not act or df5.empty:
        return info
    primary = "new_york" if "new_york" in act else act[0]
    a, b = sessions[primary]
    s, _ = _window(as_of, a, b, 0)
    seg = df5[(df5.index >= s) & (df5.index < as_of)]
    if seg.empty:
        return info
    info["high"], info["low"] = float(seg["high"].max()), float(seg["low"].min())
    info["range"] = info["high"] - info["low"]
    orng = seg[seg.index < s + pd.Timedelta(minutes=or_minutes)]
    if len(orng):
        info["opening_range"] = (float(orng["high"].max()), float(orng["low"].min()))
    # London breakout vs Asian range, New York breakout vs London range (close beyond completed range)
    completed = completed or {}
    px = float(seg["close"].iloc[-1])
    if primary == "london" and "ASIA_H" in completed and "ASIA_L" in completed:
        if px > completed["ASIA_H"]:
            info["breakout"] = "LONDON_BREAKOUT_UP"
        elif px < completed["ASIA_L"]:
            info["breakout"] = "LONDON_BREAKOUT_DOWN"
    if primary == "new_york" and "LONDON_H" in completed and "LONDON_L" in completed:
        if px > completed["LONDON_H"]:
            info["breakout"] = "NY_BREAKOUT_UP"
        elif px < completed["LONDON_L"]:
            info["breakout"] = "NY_BREAKOUT_DOWN"
    return info


def session_of(ts: pd.Timestamp, sessions: dict) -> str:
    return session_label(ts, sessions)

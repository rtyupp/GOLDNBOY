"""High-impact event filter. Events come from a CSV you maintain (data/events.csv):
datetime_utc,name,impact     e.g.  2026-07-02 12:30,NFP,high
Windows: no trading `before_min` minutes before, wait `after_min` minutes after for stabilization."""
from __future__ import annotations
import os
from dataclasses import dataclass
from typing import List, Optional
import pandas as pd

KEYWORDS = ("CPI", "NFP", "FOMC", "PCE", "FED", "UNEMPLOYMENT", "GDP", "INTEREST RATE", "POWELL", "NONFARM")


@dataclass
class NewsStatus:
    state: str                  # CLEAR | PRE_NEWS | POST_NEWS
    event: Optional[str] = None
    minutes: Optional[float] = None   # minutes to (pre) / since (post) the event
    calendar_loaded: bool = False
    upcoming: int = 0

    @property
    def blocked(self) -> bool:
        return self.state != "CLEAR"


class NewsFilter:
    def __init__(self, path: str = "data/events.csv", before_min: float = 30, after_min: float = 15):
        self.path, self.before, self.after = path, before_min, after_min
        self.events = self._load()

    def _load(self) -> pd.DataFrame:
        if not os.path.exists(self.path):
            return pd.DataFrame(columns=["dt", "name", "impact"])
        df = pd.read_csv(self.path, comment="#")
        if df.empty:
            return pd.DataFrame(columns=["dt", "name", "impact"])
        df["dt"] = pd.to_datetime(df["datetime_utc"], utc=True)
        df["impact"] = df.get("impact", "high").fillna("high").str.lower()
        keep = (df["impact"] == "high") | df["name"].str.upper().apply(lambda s: any(k in s for k in KEYWORDS))
        return df[keep][["dt", "name", "impact"]].sort_values("dt").reset_index(drop=True)

    def reload(self):
        self.events = self._load()

    def status(self, now: pd.Timestamp) -> NewsStatus:
        ev = self.events
        loaded = len(ev) > 0
        upcoming = int((ev["dt"] > now).sum()) if loaded else 0
        for _, r in ev.iterrows():
            delta = (r["dt"] - now).total_seconds() / 60.0
            if 0 <= delta <= self.before:
                return NewsStatus("PRE_NEWS", r["name"], delta, loaded, upcoming)
            if -self.after <= delta < 0:
                return NewsStatus("POST_NEWS", r["name"], -delta, loaded, upcoming)
        return NewsStatus("CLEAR", None, None, loaded, upcoming)

    def next_events(self, now: pd.Timestamp, k: int = 3) -> List[tuple]:
        ev = self.events[self.events["dt"] > now].head(k)
        return [(r["dt"], r["name"]) for _, r in ev.iterrows()]

"""Trade journal (SQLite). Every signal is stored; closed trades also feed the probability engine."""
from __future__ import annotations
import json, os, sqlite3, threading
from typing import Any, Dict, List, Optional
import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  timestamp TEXT NOT NULL, symbol TEXT, direction TEXT,
  entry REAL, sl REAL, tp1 REAL, tp2 REAL, sl_current REAL,
  strategy TEXT, session TEXT, score REAL, historical_probability REAL, prob_n INTEGER,
risk_level TEXT, reason TEXT,
  status TEXT DEFAULT 'OPEN',            -- OPEN | TP1 | CLOSED
  result TEXT, r REAL, duration_min REAL, closed_ts TEXT, tp1_ts TEXT,
  fingerprint TEXT, rr1 REAL, rr2 REAL
);
CREATE TABLE IF NOT EXISTS history_trades(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT, ts TEXT, tf TEXT, setup TEXT, direction TEXT, session TEXT, htf_bias TEXT,
  structure TEXT, atr_bucket TEXT, rsi_bucket TEXT, liquidity TEXT, regime TEXT,
  r REAL, outcome TEXT, score REAL, duration_min REAL, features TEXT
);
CREATE INDEX IF NOT EXISTS idx_ht ON history_trades(setup, direction);
"""


class Journal:
    def __init__(self, path: str = "data/journal.sqlite"):
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=20000")
        self.lock = threading.RLock()
        with self.lock:
            self.db.executescript(SCHEMA)
            self.db.commit()

    # ---------------- signals ----------------
    def add_signal(self, **k) -> int:
        cols = ["timestamp", "symbol", "direction", "entry", "sl", "tp1", "tp2", "strategy", "session", "score",
                "historical_probability", "prob_n", "risk_level", "reason",
                "fingerprint", "rr1", "rr2"]
        vals = [k.get(c) for c in cols]
        vals[cols.index("fingerprint")] = json.dumps(k.get("fingerprint") or {})
        with self.lock:
            cur = self.db.execute(
                f"INSERT INTO signals({','.join(cols)}, sl_current) VALUES({','.join('?' * len(cols))}, ?)",
                vals + [k.get("sl")])
            self.db.commit()
            return cur.lastrowid

    def open_trades(self) -> List[sqlite3.Row]:
        with self.lock:
            return list(self.db.execute("SELECT * FROM signals WHERE status IN ('OPEN','TP1') ORDER BY id"))

    def transition(self, sid: int, from_status: str, **fields) -> bool:
        """Atomic compare-and-set => each TP/SL event can fire only once."""
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.lock:
            cur = self.db.execute(f"UPDATE signals SET {sets} WHERE id=? AND status=?", list(fields.values()) + [sid, from_status])
            self.db.commit()
            return cur.rowcount == 1

    def get(self, sid: int):
        with self.lock:
            return self.db.execute("SELECT * FROM signals WHERE id=?", (sid,)).fetchone()

    def last_signals(self, n: int = 5) -> List[sqlite3.Row]:
        with self.lock:
            return list(self.db.execute("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (n,)))

    def last_signal_time(self, direction: Optional[str] = None) -> Optional[pd.Timestamp]:
        q = "SELECT timestamp FROM signals" + (" WHERE direction=?" if direction else "") + " ORDER BY id DESC LIMIT 1"
        with self.lock:
            r = self.db.execute(q, (direction,) if direction else ()).fetchone()
        return pd.Timestamp(r["timestamp"]) if r else None

    def stats(self) -> Dict[str, Any]:
        with self.lock:
            df = pd.read_sql_query("SELECT * FROM signals WHERE status='CLOSED'", self.db)
            n_open = self.db.execute("SELECT COUNT(*) c FROM signals WHERE status IN ('OPEN','TP1')").fetchone()["c"]
            total = self.db.execute("SELECT COUNT(*) c FROM signals").fetchone()["c"]
        out: Dict[str, Any] = {"total": total, "open": n_open, "closed": len(df)}
        if df.empty:
            return out
        wins, losses = df[df["r"] > 0], df[df["r"] <= 0]
        gp, gl = wins["r"].sum(), -losses["r"].sum()
        out.update(wins=len(wins), losses=len(losses), win_rate=len(wins) / len(df), avg_r=float(df["r"].mean()),
                   profit_factor=(gp / gl) if gl > 0 else float("inf"), total_r=float(df["r"].sum()),
                   tp1=int((df["result"].isin(["TP1_BE", "TP2"])).sum()), tp2=int((df["result"] == "TP2").sum()),
                   sl=int((df["result"] == "SL").sum()))
        out["by_strategy"] = df.groupby("strategy")["r"].agg(["count", "mean"]).round(2).to_dict("index")
        out["by_session"] = df.groupby("session")["r"].agg(["count", "mean"]).round(2).to_dict("index")
        return out

    # ---------------- history for probability ----------------
    def add_history(self, rows: List[dict], source: str):
        with self.lock:
            self.db.executemany(
                "INSERT INTO history_trades(source,ts,tf,setup,direction,session,htf_bias,structure,atr_bucket,rsi_bucket,liquidity,regime,r,outcome,score,duration_min,features) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(source, str(r["ts"]), r.get("tf"), r["setup"], r["direction"], r["session"], r["htf_bias"], r["structure"],
                  r["atr_bucket"], r["rsi_bucket"], r["liquidity"], r["regime"], float(r["r"]), r["outcome"],
                  r.get("score"), r.get("duration_min"), json.dumps(r.get("features", {}))) for r in rows])
            self.db.commit()

    def clear_history(self, source: str, tf: Optional[str] = None):
        with self.lock:
            if tf:
                self.db.execute("DELETE FROM history_trades WHERE source=? AND tf=?", (source, tf))
            else:
                self.db.execute("DELETE FROM history_trades WHERE source=?", (source,))
            self.db.commit()

    def history_trades(self, before: Optional[pd.Timestamp] = None) -> pd.DataFrame:
        with self.lock:
            df = pd.read_sql_query("SELECT * FROM history_trades", self.db)
        if df.empty:
            return df
        if before is not None:
            df = df[pd.to_datetime(df["ts"], utc=True) < before]   # never use trades from the future
        return df

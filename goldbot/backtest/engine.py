"""Walk-forward style event backtester using the SAME pipeline as live trading.

 - Analysis at time t only sees candles CLOSED at t (no future data).
 - Indicators are causal; structure uses confirmed pivots only.
 - Trade outcome is simulated on 1m high/low AFTER the signal; SL is checked before TP inside a bar (conservative).
 - Fixed assumed spread (history has no bid/ask). No historical news calendar => news filter is NOT applied.
 - Probability engine is NOT used inside the backtest (it would leak the test into itself).
Results are an estimate of the past, not a guarantee for the future.
"""
from __future__ import annotations
import json, logging, os
from dataclasses import dataclass
from typing import Dict, List, Optional
import numpy as np
import pandas as pd

from goldbot.analysis.indicators import add_indicators
from goldbot.analysis.sessions import session_label
from goldbot.core.timeframes import resample_ohlcv, closed_only, ALL_TFS, TF_MINUTES
from goldbot.engine.confluence import confluence
from goldbot.engine.pipeline import Pipeline
from goldbot.ml.model import features as ml_features

log = logging.getLogger("backtest")


def simulate_trade(plan, t0: pd.Timestamp, arr: dict, partial: float, spread: float, max_minutes: int,
                   slippage: float = 0.0):
    """Walk 1m bars starting at t0. Returns (result, r, duration_min, close_ts)."""
    idx = arr["index"]
    i0 = int(idx.searchsorted(t0, side="left"))
    buy = plan.direction == "BUY"
    sl_cur, state = plan.sl, "OPEN"
    half = spread / 2.0
    entry = plan.entry + (slippage if buy else -slippage)
    risk = abs(entry - plan.sl)
    if risk <= 0:
        return "SL", -1.0, 1, idx[i0] + pd.Timedelta(minutes=1)
    hi, lo, cl = arr["high"], arr["low"], arr["close"]
    n = len(idx)
    end = min(n, i0 + max_minutes)
    for i in range(i0, end):
        # bid (for longs) / ask (for shorts) is the price we would close at
        worst = lo[i] - half if buy else hi[i] + half          # adverse extreme
        best = hi[i] - half if buy else lo[i] + half           # favourable extreme
        sl_hit = worst <= sl_cur if buy else worst >= sl_cur
        tp1_hit = best >= plan.tp1 if buy else best <= plan.tp1
        tp2_hit = best >= plan.tp2 if buy else best <= plan.tp2
        dur = (i - i0 + 1)
        ts = idx[i] + pd.Timedelta(minutes=1)
        if state == "OPEN":
            if sl_hit:
                return "SL", -1.0, dur, ts
            if tp2_hit:
                return "TP2", partial * plan.rr1 + (1 - partial) * plan.rr2, dur, ts
            if tp1_hit:
                state, sl_cur = "TP1", entry
                if (lo[i] - half <= sl_cur) if buy else (hi[i] + half >= sl_cur):
                    pass          # BE touched in same bar after TP1: resolved on next bar to stay conservative-neutral
        else:
            if sl_hit:
                return "TP1_BE", partial * plan.rr1, dur, ts
            if tp2_hit:
                return "TP2", partial * plan.rr1 + (1 - partial) * plan.rr2, dur, ts
    last = min(end, n) - 1
    px = cl[last] - half if buy else cl[last] + half
    move = (px - entry) if buy else (entry - px)
    r = move / risk if state == "OPEN" else partial * plan.rr1 + (1 - partial) * move / risk
    return "EXPIRED", float(r), last - i0 + 1, idx[last] + pd.Timedelta(minutes=1)


def metrics(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"total_trades": 0}
    r = df["r"].values
    wins, losses = r[r > 0], r[r <= 0]
    eq = np.cumsum(r)
    dd = float((np.maximum.accumulate(eq) - eq).max()) if len(eq) else 0.0
    gl = -losses.sum()
    n = len(df)
    return {"total_trades": n, "wins": int((r > 0).sum()), "losses": int((r <= 0).sum()),
            "win_rate": round(float((r > 0).mean()), 4), "avg_r": round(float(r.mean()), 3),
            "expectancy_r": round(float(r.mean()), 3), "profit_factor": round(float(wins.sum() / gl), 2) if gl > 0 else None,
            "max_drawdown_r": round(dd, 2), "total_r": round(float(r.sum()), 2),
            "tp1_pct": round(float(df["outcome"].isin(["TP1_BE", "TP2"]).mean()), 3),
            "tp2_pct": round(float((df["outcome"] == "TP2").mean()), 3),
            "sl_pct": round(float((df["outcome"] == "SL").mean()), 3)}


def breakdown(df: pd.DataFrame, col: str) -> dict:
    return {str(k): metrics(g) for k, g in df.groupby(col)} if not df.empty else {}


def split_report(df: pd.DataFrame, frac: float = 0.7) -> dict:
    """Chronological in-sample / out-of-sample split of the trade list."""
    if len(df) < 10:
        return {}
    d = df.sort_values("ts")
    k = int(len(d) * frac)
    return {"in_sample": metrics(d.iloc[:k]), "out_of_sample": metrics(d.iloc[k:])}


def run_backtest(base: pd.DataFrame, cfg, eval_tf: str = "5m", step: int = 1, max_evals: Optional[int] = None,
                 warmup_days: int = 12, only: Optional[List[str]] = None, progress=None) -> dict:
    spread = float(cfg.get("backtest.spread", 0.3))
    slippage = float(cfg.get("backtest.slippage", 0.0))
    partial = float(cfg.get("tracking.tp1_partial_pct", 0.5))
    max_minutes = int(cfg.get("tracking.max_trade_minutes", 1440))
    min_conf = float(cfg.get("confluence.min_score", 70))
    pipe = Pipeline(cfg, journal=None, only=only)
    sessions = pipe.analyzer.sessions
    use_ta = pipe.analyzer.use_ta
    full = {tf: resample_ohlcv(base, tf) for tf in ALL_TFS}
    ind = {tf: add_indicators(full[tf].drop(columns="close_time"), use_ta).join(full[tf][["close_time"]]) for tf in full}
    arr = {"index": base.index, "high": base["high"].values, "low": base["low"].values, "close": base["close"].values}
    ev = ind[eval_tf]
    start_ts = base.index[0] + pd.Timedelta(days=warmup_days)
    ev = ev[ev["close_time"] >= start_ts]
    ev = ev.iloc[::step]
    if max_evals:
        ev = ev.iloc[-max_evals:]
    names = [s.label for s in pipe.strategies]
    books: Dict[str, dict] = {n: {"busy": pd.Timestamp(0, tz="UTC"), "trades": []} for n in names + ["ALL"]}
    total = len(ev)
    for k, (_, row) in enumerate(ev.iterrows()):
        as_of = row["close_time"]
        if all(as_of < b["busy"] for b in books.values()):
            continue
        frames = {tf: closed_only(ind[tf], as_of) for tf in ind}
        px = float(frames["1m"]["close"].iloc[-1]) if len(frames["1m"]) else float(row["close"])
        cand = pipe.prepare(frames, px - spread / 2, px + spread / 2, as_of, True, "OK", use_probability=False)
        if progress and k % 500 == 0:
            progress(k, total)
        if cand.ctx is None or cand.global_reasons:
            continue
        ctx = cand.ctx
        confs: Dict[str, tuple] = {}
        for r in cand.results:
            if r.signal == "NONE" or as_of < books[r.name]["busy"]:
                continue
            d = r.setup_tags["direction"]
            if d not in confs:
                confs[d] = confluence(ctx, d)
            if confs[d][0] < min_conf:
                continue
            _record(books[r.name], r, ctx, confs[d][0], as_of, arr, partial, spread, slippage, max_minutes, sessions, eval_tf)
        if cand.tradable and as_of >= books["ALL"]["busy"]:
            _record(books["ALL"], cand.best, ctx, cand.confluence, as_of, arr, partial, spread, slippage, max_minutes, sessions, eval_tf)
    out = {"eval_tf": eval_tf, "evals": total, "spread_assumed": spread, "slippage_assumed": slippage, "strategies": {}, "books": {}}
    for n, b in books.items():
        df = pd.DataFrame(b["trades"])
        b["df"] = df
        rep = metrics(df)
        if not df.empty:
            rep["by_session"] = breakdown(df, "session")
            rep["by_setup"] = breakdown(df, "setup")
            rep["by_regime"] = breakdown(df, "regime")
            rep["walk_forward_split"] = split_report(df)
        out["strategies" if n != "ALL" else "combined"] = out["strategies"] if n != "ALL" else rep
        if n != "ALL":
            out["strategies"][n] = rep
    out["books"] = {n: b["df"] for n, b in books.items()}
    return out


def _record(book, res, ctx, conf, as_of, arr, partial, spread, slippage, max_minutes, sessions, eval_tf):
    plan = res.plan
    outcome, r, dur, close_ts = simulate_trade(plan, as_of, arr, partial, spread, max_minutes, slippage)
    book["busy"] = close_ts
    from goldbot.engine.probability import fingerprint
    fp = fingerprint(ctx, res)
    fp.update(ts=as_of.isoformat(), tf=eval_tf, r=float(r), outcome=outcome, score=float(conf),
              duration_min=float(dur), features=ml_features(ctx, res, conf))
    book["trades"].append(fp)


def save_report(result: dict, out_dir: str, tag: str = "") -> str:
    os.makedirs(out_dir, exist_ok=True)
    tf = result["eval_tf"]
    summary = {k: v for k, v in result.items() if k != "books"}
    path = os.path.join(out_dir, f"backtest_{tf}{tag}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, default=str)
    for n, df in result["books"].items():
        if not df.empty:
            df.drop(columns=["features"], errors="ignore").to_csv(os.path.join(out_dir, f"trades_{tf}_{n.replace(' ', '_')}{tag}.csv"), index=False)
    return path


def store_probability_history(journal, result: dict):
    """Per-strategy backtest trades become the historical sample for the probability engine."""
    tf = result["eval_tf"]
    journal.clear_history("backtest", tf)
    n = 0
    for name, df in result["books"].items():
        if name == "ALL" or df.empty:
            continue
        journal.add_history(df.to_dict("records"), "backtest")
        n += len(df)
    return n


def vectorbt_cross_check(trades: pd.DataFrame, close: pd.Series):
    """Optional: re-evaluate entries/exits with vectorbt (only if installed). Not used by the live path."""
    try:
        import vectorbt as vbt
    except Exception:
        return None
    t = trades.copy()
    t["ts"] = pd.to_datetime(t["ts"], utc=True)
    entries = pd.Series(False, index=close.index)
    exits = pd.Series(False, index=close.index)
    for _, r in t.iterrows():
        i = close.index.searchsorted(r["ts"])
        if i < len(close):
            entries.iloc[i] = True
            j = min(len(close) - 1, i + max(1, int(r["duration_min"])))
            exits.iloc[j] = True
    pf = vbt.Portfolio.from_signals(close, entries, exits, direction="both" if t["direction"].nunique() > 1 else "longonly")
    return pf.stats()

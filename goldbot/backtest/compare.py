"""مقارنة واقعية: الاستراتيجيات الحالية (الخمس) مقابل الحالية + SMC، على نفس البيانات ونفس التكاليف (سبريد + انزلاق).
وتقارن أيضًا «بدون تكاليف» مقابل «مع تكاليف» لإظهار أثر السبريد والانزلاق.

القاعدة المعتمدة لقبول أي استراتيجية جديدة (طباعة فقط؛ القرار لك):
  ≥ MIN_TRADES صفقة، متوسط R > 0، معامل الربح > 1، ومتوسط R خارج العينة (آخر 30% زمنيًا) > 0.
النتائج على بيانات صناعية لا تثبت ربحية؛ شغّل الأمر على بيانات Dukascopy الحقيقية قبل الاعتماد."""
from __future__ import annotations
import copy
import json
import os
from typing import Optional

import pandas as pd

from goldbot.backtest.engine import run_backtest
from goldbot.config import Cfg
from goldbot.strategies.impl import ALL

BASELINE_KEYS = ["trend_pullback", "liquidity_sweep", "breakout", "reversal", "vwap"]
NEW_KEYS = ["smc_ote_fvg", "smc_sweep_mss_fvg"]
ADVANCED_KEYS = ["liquidity_grab", "silver_bullet"]
MIN_TRADES = 30


def _cfg(cfg: Cfg, spread: float, slip_e: float, slip_s: float) -> Cfg:
    c = Cfg(copy.deepcopy(cfg.data))
    c.set("backtest.spread", spread)
    c.set("backtest.slippage_entry", slip_e)
    c.set("backtest.slippage_stop", slip_s)
    return c


def verdict(rep: dict) -> str:
    n = rep.get("total_trades", 0)
    if n < MIN_TRADES:
        return f"عينة غير كافية ({n}<{MIN_TRADES})"
    oos = (rep.get("walk_forward_split") or {}).get("out_of_sample", {})
    folds = rep.get("walk_forward_folds") or {}
    # A single 70/30 split can hide an edge that only exists on one slice; when folds are
    # available they must agree (majority of folds positive) as well.
    fold_ok = folds.get("consistent", True)
    ok = rep.get("avg_r", 0) > 0 and (rep.get("profit_factor") or 0) > 1 and oos.get("avg_r", -1) > 0 and fold_ok
    return "مقبولة ✅" if ok else "مرفوضة ❌"


def compare(base: pd.DataFrame, cfg: Cfg, tf: str = "5m", step: int = 1, max_evals: Optional[int] = None,
            warmup_days: int = 12, progress=None) -> dict:
    sp = float(cfg.get("backtest.spread", 0.3))
    se = float(cfg.get("backtest.slippage_entry", 0.1))
    ss = float(cfg.get("backtest.slippage_stop", 0.15))
    real = _cfg(cfg, sp, se, ss)
    ideal = _cfg(cfg, 0.0, 0.0, 0.0)
    runs = {
        "baseline_real": run_backtest(base, real, tf, step, max_evals, warmup_days, only=BASELINE_KEYS, progress=progress),
        "extended_real": run_backtest(base, real, tf, step, max_evals, warmup_days, only=BASELINE_KEYS + NEW_KEYS, progress=progress),
        "advanced_real": run_backtest(base, real, tf, step, max_evals, warmup_days, only=BASELINE_KEYS + NEW_KEYS + ADVANCED_KEYS, progress=progress),
        "extended_ideal": run_backtest(base, ideal, tf, step, max_evals, warmup_days, only=BASELINE_KEYS + NEW_KEYS + ADVANCED_KEYS, progress=progress),
    }
    labels = {c.key: c.label for c in ALL}
    out = {"tf": tf, "costs": {"spread": sp, "slippage_entry": se, "slippage_stop": ss}, "runs": {}, "verdicts": {}}
    for k, r in runs.items():
        out["runs"][k] = {"combined": r.get("combined"), "strategies": dict(r["strategies"])}
    adv = runs["advanced_real"]["strategies"]
    for key in NEW_KEYS + ADVANCED_KEYS:
        out["verdicts"][labels[key]] = verdict(adv.get(labels[key], {}))
    return out


def fmt(res: dict) -> str:
    def row(name, r):
        if not r or not r.get("total_trades"):
            return f"  {name:22s} صفقات=0"
        pf = r.get("profit_factor")
        return (f"  {name:22s} صفقات={r['total_trades']:4d} ربح={r['win_rate']:.1%} متوسط R={r['avg_r']:+.3f} "
                f"PF={pf if pf is not None else '-'} أقصى تراجع R={r['max_drawdown_r']}")
    c = res["costs"]
    lines = [f"المقارنة على فريم {res['tf']} — التكاليف الواقعية: سبريد {c['spread']} + انزلاق دخول {c['slippage_entry']} + انزلاق وقف {c['slippage_stop']}"]
    for k, title in (("baseline_real", "الاستراتيجيات الحالية فقط (مع تكاليف)"), ("extended_real", "الحالية + SMC (مع تكاليف)"),
                     ("advanced_real", "الحالية + SMC + المتقدمة (مع تكاليف)"),
                     ("extended_ideal", "الكل (بدون أي تكاليف — للمقارنة فقط)")):
        lines.append(f"\n{title}")
        r = res["runs"][k]
        lines.append(row("المجمّع (الإشارة المرسلة)", r["combined"]))
        for n, v in r["strategies"].items():
            lines.append(row(n, v))
    lines.append("\nحكم الاستراتيجيات الجديدة والمتقدمة (مع التكاليف):")
    for n, v in res["verdicts"].items():
        lines.append(f"  {n}: {v}")
    return "\n".join(lines)


def save(res: dict, out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"compare_{res['tf']}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(res, f, indent=2, default=str, ensure_ascii=False)
    return path

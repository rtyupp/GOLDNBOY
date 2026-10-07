"""Optional ML gate. Trains ONLY on historical trades (features captured at signal time),
chronological TRAIN -> VALIDATION -> OUT-OF-SAMPLE TEST split with a purge gap (no leakage).
The model is activated only if its out-of-sample AUC beats the threshold; otherwise it stays off."""
from __future__ import annotations
import json, logging, os, pickle
from typing import Dict, Optional
import numpy as np
import pandas as pd

log = logging.getLogger("ml")
SESS = {"Asian": 0, "London": 1, "London/New York": 2, "New York": 3, "Off-hours": 4}
TR = {"BULLISH": 1, "BEARISH": -1, "RANGE": 0, "TRANSITION": 0}


def features(ctx, res, conf: float) -> Dict[str, float]:
    s5, s15 = ctx.tf["5m"], ctx.tf["15m"]
    l = s5.last
    d = res.setup_tags.get("direction", "bull")
    sgn = 1 if d == "bull" else -1
    a = max(s5.atr, 1e-9)
    atr_s = s5.df["atr"].dropna().iloc[-300:]
    lb = s5.last_break
    sweep = any(s.expected == d and s.bars_ago <= 12 for s in s5.sweeps + s15.sweeps)
    return {
        "dir": sgn, "rsi": float(l["rsi"]), "rsi15": float(s15.last["rsi"]),
        "atr_pct": float((atr_s < s5.atr).mean()) if len(atr_s) > 20 else 0.5, "atr_rel_price": a / float(l["close"]) * 1e3,
        "ema20_dist": sgn * (float(l["close"]) - float(l["ema_20"])) / a, "ema50_dist": sgn * (float(l["close"]) - float(l["ema_50"])) / a,
        "ema200_dist": sgn * (float(l["close"]) - float(l["ema_200"])) / a if l["ema_200"] == l["ema_200"] else 0.0,
        "vwap_dist": sgn * (float(l["close"]) - float(l["vwap"])) / a, "rel_vol": float(l["rel_vol"]) if l["rel_vol"] == l["rel_vol"] else 1.0,
        "bb_width": float(l["bb_width"]), "er": float(s15.last["er"]), "macd_hist": sgn * float(l["macd_hist"]) / a,
        "session": SESS.get(ctx.session["label"], 4), "t4h": sgn * TR[ctx.tf["4H"].trend], "t1h": sgn * TR[ctx.tf["1H"].trend],
        "t15": sgn * TR[s15.trend], "t5": sgn * TR[s5.trend],
        "structure_break": (1 if (lb and lb.direction == d) else (-1 if lb else 0)), "liquidity_sweep": int(sweep),
        "body_ratio": abs(float(l["close"] - l["open"])) / max(float(l["high"] - l["low"]), 1e-9),
        "conf": conf, "strategy_score": res.score, "rr2": res.plan.rr2, "risk_atr": res.plan.risk / max(ctx.atr, 1e-9),
    }


def _make_model():
    try:
        import lightgbm as lgb
        return lgb.LGBMClassifier(n_estimators=200, learning_rate=0.05, num_leaves=15, min_child_samples=20, verbose=-1), "lightgbm"
    except Exception:
        pass
    try:
        import xgboost as xgb
        return xgb.XGBClassifier(n_estimators=200, max_depth=3, learning_rate=0.05, subsample=0.8, eval_metric="logloss"), "xgboost"
    except Exception:
        pass
    from sklearn.ensemble import RandomForestClassifier
    return RandomForestClassifier(n_estimators=300, min_samples_leaf=10, max_depth=6, random_state=7, n_jobs=-1), "random_forest"


class MLGate:
    def __init__(self, cfg):
        self.cfg = cfg
        self.path = cfg.get("ml.model_path", "data/ml_model.pkl")
        self.min_auc = float(cfg.get("ml.min_oos_auc", 0.55))
        self.min_test = int(cfg.get("ml.min_test_samples", 50))
        self.model = None
        self.cols = None
        self.report: dict = {}
        self.active = False
        if cfg.get("ml.enabled", False) and os.path.exists(self.path):
            self.load()

    def load(self):
        with open(self.path, "rb") as f:
            blob = pickle.load(f)
        self.model, self.cols, self.report = blob["model"], blob["cols"], blob["report"]
        self.active = bool(self.report.get("passed"))
        log.info("ML model loaded (%s) active=%s oos_auc=%s", self.report.get("kind"), self.active, self.report.get("test_auc"))

    def predict_win_prob(self, ctx, res, conf) -> Optional[float]:
        if not self.active:
            return None
        f = features(ctx, res, conf)
        x = pd.DataFrame([[f.get(c, 0.0) for c in self.cols]], columns=self.cols)
        return float(self.model.predict_proba(x)[0, 1])

    def train(self, history: pd.DataFrame) -> dict:
        from sklearn.metrics import roc_auc_score
        rep: dict = {"passed": False}
        df = history.copy()
        df = df[df["features"].notna() & (df["features"] != "{}")]
        if len(df) < 200:
            rep["error"] = f"need >=200 trades with features, have {len(df)}"
            self.report = rep
            return rep
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
        df = df.sort_values("ts").reset_index(drop=True)
        X = pd.DataFrame([json.loads(s) for s in df["features"]]).fillna(0.0)
        y = (df["r"] > 0).astype(int).values
        n = len(df)
        a, b = int(n * 0.6), int(n * 0.8)
        gap = max(1, int(n * 0.01))                      # purge gap between folds (trades overlap in time)
        tr, va, te = slice(0, a - gap), slice(a, b - gap), slice(b, n)
        model, kind = _make_model()
        if len(set(y[tr])) < 2:
            rep["error"] = "training fold has a single class"
            self.report = rep
            return rep
        model.fit(X.iloc[tr], y[tr])

        def auc(sl):
            return float(roc_auc_score(y[sl], model.predict_proba(X.iloc[sl])[:, 1])) if len(set(y[sl])) > 1 else float("nan")
        rep.update(kind=kind, n=n, train_n=a - gap, val_n=b - a - gap, test_n=n - b, train_auc=auc(tr), val_auc=auc(va),
                   test_auc=auc(te), train_end=str(df["ts"].iloc[a - gap - 1]), test_start=str(df["ts"].iloc[b]),
                   base_win_rate_test=float(y[te].mean()))
        rep["passed"] = bool(rep["test_n"] >= self.min_test and rep["test_auc"] >= self.min_auc and rep["val_auc"] >= self.min_auc)
        self.model, self.cols, self.report, self.active = model, list(X.columns), rep, rep["passed"]
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "wb") as f:
            pickle.dump({"model": model, "cols": self.cols, "report": rep}, f)
        return rep

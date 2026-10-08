"""Technical indicators. Uses the `ta` library (bukosabino/ta) when installed, otherwise an
internal implementation of the same standard formulas (so the bot still runs).
All indicators are causal (row i only uses rows <= i) => no look-ahead."""
from __future__ import annotations
import numpy as np
import pandas as pd

try:
    import ta as _ta
    HAVE_TA = True
except Exception:  # pragma: no cover
    _ta = None
    HAVE_TA = False


def _wilder(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def _rsi(close, n=14):
    d = close.diff()
    up, dn = _wilder(d.clip(lower=0), n), _wilder((-d).clip(lower=0), n)
    rs = up / dn.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    out = out.where(~((dn == 0) & (up > 0)), 100.0)      # no losses at all => RSI 100
    out = out.where(~((dn == 0) & (up == 0)), 50.0)      # flat => 50
    return out


def _atr(h, l, c, n=14):
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return _wilder(tr, n)


def add_vwap(df: pd.DataFrame) -> pd.Series:
    """VWAP reset every UTC day; volume = tick count (spot gold has no real volume)."""
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    vol = df["volume"].replace(0, np.nan).fillna(1.0)
    day = df.index.floor("1D")
    pv = (tp * vol).groupby(day).cumsum()
    vv = vol.groupby(day).cumsum()
    return pv / vv


_TA_BROKEN = False


def _ta_block(d, c, h, l, use_ta):
    """Indicators from the `ta` library. Raises on any problem so the caller can fall back."""
    for n in (9, 20, 50, 100, 200):
        d[f"ema_{n}"] = _ta.trend.EMAIndicator(c, window=n, fillna=False).ema_indicator()
    d["sma_200"] = _ta.trend.SMAIndicator(c, window=200, fillna=False).sma_indicator()
    d["rsi"] = _ta.momentum.RSIIndicator(c, window=14, fillna=False).rsi()
    m = _ta.trend.MACD(c, window_slow=26, window_fast=12, window_sign=9, fillna=False)
    d["macd"], d["macd_signal"], d["macd_hist"] = m.macd(), m.macd_signal(), m.macd_diff()
    st = _ta.momentum.StochasticOscillator(h, l, c, window=14, smooth_window=3, fillna=False)
    d["stoch_k"], d["stoch_d"] = st.stoch(), st.stoch_signal()
    d["roc"] = _ta.momentum.ROCIndicator(c, window=12, fillna=False).roc()
    d["atr"] = _ta.volatility.AverageTrueRange(h, l, c, window=14, fillna=False).average_true_range()
    bb = _ta.volatility.BollingerBands(c, window=20, window_dev=2, fillna=False)
    d["bb_high"], d["bb_low"], d["bb_mid"] = bb.bollinger_hband(), bb.bollinger_lband(), bb.bollinger_mavg()


def _internal_block(d, c, h, l):
    for n in (9, 20, 50, 100, 200):
        d[f"ema_{n}"] = c.ewm(span=n, adjust=False, min_periods=n).mean()
    d["sma_200"] = c.rolling(200).mean()
    d["rsi"] = _rsi(c, 14)
    ef, es = c.ewm(span=12, adjust=False).mean(), c.ewm(span=26, adjust=False).mean()
    d["macd"] = ef - es
    d["macd_signal"] = d["macd"].ewm(span=9, adjust=False).mean()
    d["macd_hist"] = d["macd"] - d["macd_signal"]
    ll, hh = l.rolling(14).min(), h.rolling(14).max()
    d["stoch_k"] = 100 * (c - ll) / (hh - ll).replace(0, np.nan)
    d["stoch_d"] = d["stoch_k"].rolling(3).mean()
    d["roc"] = 100 * (c / c.shift(12) - 1)
    d["atr"] = _atr(h, l, c, 14)
    d["bb_mid"] = c.rolling(20).mean()
    sd = c.rolling(20).std(ddof=0)
    d["bb_high"], d["bb_low"] = d["bb_mid"] + 2 * sd, d["bb_mid"] - 2 * sd


def add_indicators(df: pd.DataFrame, use_ta: bool = True) -> pd.DataFrame:
    global _TA_BROKEN
    if df.empty:
        return df
    d = df.copy()
    c, h, l, v = d["close"], d["high"], d["low"], d["volume"]
    done = False
    # ta's long windows (EMA/SMA 200) are not meaningful on a short startup
    # frame.  Do not call it there: a one-off short-data IndexError must not
    # permanently disable ta for the rest of the process.
    if use_ta and HAVE_TA and not _TA_BROKEN and len(d) >= 200:
        try:
            _ta_block(d, c, h, l, True)
            done = True
        except Exception as e:       # incompatible ta/pandas combination -> permanent safe fallback
            _TA_BROKEN = True
            import logging
            logging.getLogger("indicators").warning("`ta` library failed (%s: %s) -> using built-in formulas", type(e).__name__, e)
    if not done:
        _internal_block(d, c, h, l)
    d["std"] = c.rolling(20).std(ddof=0)
    d["bb_width"] = (d["bb_high"] - d["bb_low"]) / d["bb_mid"]
    d["vol"] = v
    d["rel_vol"] = v / v.rolling(20).mean().replace(0, np.nan)
    d["vwap"] = add_vwap(d)
    # Kaufman efficiency ratio (trendiness 0..1) - used by the choppy-market gate
    net = (c - c.shift(20)).abs()
    path = c.diff().abs().rolling(20).sum()
    d["er"] = (net / path.replace(0, np.nan)).fillna(0.0)
    return d

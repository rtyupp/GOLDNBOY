"""Providers are swapped from config.yaml (providers.live / providers.history).

Everything used by default is FREE and needs no API key / account:
  live    -> Swissquote public quotes (bid/ask polling, candles built locally)
  history -> Dukascopy hourly tick files (converted to 1m OHLCV)
OANDA Practice (free demo account) stays available as an optional alternative.
"""
from __future__ import annotations
import logging
from goldbot.config import Cfg, env

log = logging.getLogger("providers")


def _oanda_ready() -> bool:
    return bool(env("OANDA_API_TOKEN") and env("OANDA_ACCOUNT_ID"))


def make_live_provider(cfg: Cfg, on_tick, on_state):
    kind = cfg.get("providers.live", "swissquote_public")
    if kind in ("oanda", "oanda_stream"):
        if _oanda_ready():
            from goldbot.providers.oanda import OandaStream
            return OandaStream(cfg.section("providers.oanda"), env("OANDA_API_TOKEN"), env("OANDA_ACCOUNT_ID"),
                               cfg.get("providers.oanda.instrument", "XAU_USD"), on_tick, on_state)
        log.warning("OANDA credentials missing; using free Swissquote public quotes instead")
        kind = "swissquote_public"
    if kind in ("swissquote", "swissquote_public"):
        from goldbot.providers.swissquote import SwissquotePublic
        return SwissquotePublic(cfg.section("providers.swissquote"), cfg.get("symbol", "XAUUSD"), on_tick, on_state)
    raise ValueError(f"unknown live provider: {kind} (supported: swissquote_public, oanda)")


def make_history_provider(cfg: Cfg):
    kind = cfg.get("providers.history", "dukascopy")
    sym = cfg.get("symbol", "XAUUSD")
    if kind in ("oanda", "oanda_rest"):
        if _oanda_ready():
            from goldbot.providers.oanda import OandaHistory
            return OandaHistory(cfg.section("providers.oanda"), env("OANDA_API_TOKEN"), env("OANDA_ACCOUNT_ID"),
                                cfg.get("providers.oanda.instrument", "XAU_USD"))
        log.warning("OANDA credentials missing; using free Dukascopy history instead")
        kind = "dukascopy"
    if kind in ("dukascopy", "dukascopy_free"):
        from goldbot.providers.dukascopy import DukascopyHistory
        return DukascopyHistory(cfg.section("providers.dukascopy"), sym)
    if kind == "csv":
        from goldbot.providers.csv_history import CsvHistory
        return CsvHistory(cfg.section("providers.csv"))
    if kind == "synthetic":
        from goldbot.providers.synthetic import SyntheticHistory
        return SyntheticHistory(cfg.section("providers.synthetic"))
    raise ValueError(f"unknown history provider: {kind} (supported: dukascopy, oanda, csv, synthetic)")

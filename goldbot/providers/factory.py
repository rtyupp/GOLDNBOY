"""Providers are swapped from config.yaml (providers.live / providers.history)."""
from __future__ import annotations
from goldbot.config import Cfg, env


def _oanda_ready() -> bool:
    return bool(env("OANDA_API_TOKEN") and env("OANDA_ACCOUNT_ID"))


def make_live_provider(cfg: Cfg, on_tick, on_state):
    kind = cfg.get("providers.live", "siftingio_ws")
    if kind in ("swissquote", "swissquote_public"):
        from goldbot.providers.swissquote import SwissquotePublic
        return SwissquotePublic(cfg.section("providers.swissquote"), cfg.get("symbol", "XAUUSD"), on_tick, on_state)
    if kind in ("oanda", "oanda_stream") and _oanda_ready():
        from goldbot.providers.oanda import OandaStream
        return OandaStream(cfg.section("providers.oanda"), env("OANDA_API_TOKEN"), env("OANDA_ACCOUNT_ID"),
                           cfg.get("providers.oanda.instrument", "XAU_USD"), on_tick, on_state)
    if kind in ("oanda", "oanda_stream") and env("SIFTING_API_KEY"):
        import logging
        logging.getLogger("providers").warning("OANDA credentials missing; falling back to SiftingIO")
        kind = "siftingio_ws"
    if kind == "siftingio_ws":
        from goldbot.providers.siftingio_ws import SiftingWSProvider
        key = env("SIFTING_API_KEY")
        if not key:
            raise RuntimeError("SIFTING_API_KEY is not set")
        return SiftingWSProvider(cfg.section("providers.siftingio"), key, cfg.get("symbol", "XAUUSD"), on_tick, on_state)
    raise ValueError(f"unknown live provider: {kind}")


def make_history_provider(cfg: Cfg):
    kind = cfg.get("providers.history", "siftingio_rest")
    if kind in ("oanda", "oanda_rest") and _oanda_ready():
        from goldbot.providers.oanda import OandaHistory
        return OandaHistory(cfg.section("providers.oanda"), env("OANDA_API_TOKEN"), env("OANDA_ACCOUNT_ID"),
                            cfg.get("providers.oanda.instrument", "XAU_USD"))
    if kind in ("oanda", "oanda_rest") and env("SIFTING_API_KEY"):
        import logging
        logging.getLogger("providers").warning("OANDA credentials missing; falling back to SiftingIO history")
        kind = "siftingio_rest"
    if kind == "siftingio_rest":
        from goldbot.providers.siftingio_rest import SiftingRestHistory
        key = env("SIFTING_API_KEY")
        if not key:
            raise RuntimeError("SIFTING_API_KEY is not set")
        return SiftingRestHistory(cfg.section("providers.siftingio"), key, cfg.get("symbol", "XAUUSD"))
    if kind == "csv":
        from goldbot.providers.csv_history import CsvHistory
        return CsvHistory(cfg.section("providers.csv"))
    if kind == "synthetic":
        from goldbot.providers.synthetic import SyntheticHistory
        return SyntheticHistory(cfg.section("providers.synthetic"))
    raise ValueError(f"unknown history provider: {kind}")

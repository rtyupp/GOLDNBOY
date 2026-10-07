"""Providers are swapped from config.yaml (providers.live / providers.history)."""
from __future__ import annotations
from goldbot.config import Cfg, env


def make_live_provider(cfg: Cfg, on_tick, on_state):
    kind = cfg.get("providers.live", "siftingio_ws")
    if kind == "siftingio_ws":
        from goldbot.providers.siftingio_ws import SiftingWSProvider
        key = env("SIFTING_API_KEY")
        if not key:
            raise RuntimeError("SIFTING_API_KEY is not set")
        return SiftingWSProvider(cfg.section("providers.siftingio"), key, cfg.get("symbol", "XAUUSD"), on_tick, on_state)
    raise ValueError(f"unknown live provider: {kind}")


def make_history_provider(cfg: Cfg):
    kind = cfg.get("providers.history", "siftingio_rest")
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

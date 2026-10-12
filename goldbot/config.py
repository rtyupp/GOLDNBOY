from __future__ import annotations
import os, copy
from typing import Any
import yaml


def deep_update(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_update(base[k], v)
        else:
            base[k] = v
    return base


class Cfg:
    """Dict wrapper with dotted access: cfg.get('data.max_tick_age_sec', 15)."""

    def __init__(self, data: dict):
        self.data = data

    def get(self, path: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def section(self, path: str) -> dict:
        v = self.get(path, {})
        return v if isinstance(v, dict) else {}

    def set(self, path: str, value: Any):
        parts = path.split(".")
        cur = self.data
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = value


def load_config(path: str = "config/config.yaml", overrides: dict | None = None, profile: str | None = None) -> Cfg:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    p = (profile or env("GOLDBOT_PROFILE") or "").strip()
    if p:
        # Profile files live next to config.yaml and only override the keys they declare.
        ppath = os.path.join(os.path.dirname(path) or ".", f"{p}.yaml")
        if not os.path.exists(ppath):
            raise FileNotFoundError(f"ملف الوضع غير موجود: {ppath}")
        with open(ppath, "r", encoding="utf-8") as f:
            deep_update(data, yaml.safe_load(f) or {})
    if overrides:
        deep_update(data, copy.deepcopy(overrides))
    data["_config_path"] = path
    data["_profile"] = p or "default"
    return Cfg(data)


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default

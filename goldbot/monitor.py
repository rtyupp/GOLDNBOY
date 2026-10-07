"""Structured logging: every analysis cycle states exactly why it did / did not trade."""
from __future__ import annotations
import json, logging, os
from logging.handlers import RotatingFileHandler
from dataclasses import dataclass, field
from typing import Dict, List, Optional


def setup_logging(level: str = "INFO", log_dir: str = "logs") -> None:
    os.makedirs(log_dir, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
    try:
        fh = RotatingFileHandler(os.path.join(log_dir, "bot.log"), maxBytes=5_000_000,
                                 backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError:
        pass


STAGES = ["DATA", "CANDLES", "INDICATORS", "STRUCTURE", "LIQUIDITY", "STRATEGY",
          "SCORE", "PROBABILITY", "RISK", "AI"]


@dataclass
class CycleLog:
    ts: str = ""
    stages: Dict[str, str] = field(default_factory=dict)
    final: str = "NO TRADE"
    reasons: List[str] = field(default_factory=list)
    extra: Dict[str, object] = field(default_factory=dict)

    def set(self, stage: str, value: str):
        self.stages[stage] = value

    def block(self) -> str:
        from goldbot.i18n import STAGE, final_ar, reasons_ar
        lines = [self.ts, ""]
        for st in STAGES:
            if st in self.stages:
                lines.append(f"{STAGE[st]}: {self.stages[st]}")
        lines.append("")
        lines.append(f"القرار النهائي: {final_ar(self.final)}")
        if self.reasons:
            lines.append("السبب: " + reasons_ar(self.reasons))
        return "\n".join(lines)

    def to_json(self) -> str:
        return json.dumps({"ts": self.ts, "stages": self.stages, "final": self.final,
                           "reasons": self.reasons, "extra": self.extra},
                          default=str, ensure_ascii=False)


class CycleRecorder:
    def __init__(self, log_dir: str = "logs"):
        os.makedirs(log_dir, exist_ok=True)
        self.path = os.path.join(log_dir, "cycles.jsonl")
        self.log = logging.getLogger("cycle")
        self.last: Optional[CycleLog] = None

    def record(self, c: CycleLog):
        self.last = c
        self.log.info("\n" + c.block())
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(c.to_json() + "\n")
        except OSError:
            pass

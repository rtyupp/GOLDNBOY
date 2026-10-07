"""Live TP1 / TP2 / SL tracking from the tick stream. Each event is sent exactly once (DB compare-and-set)."""
from __future__ import annotations
import logging, time
from typing import Callable, Optional
import pandas as pd
from goldbot.i18n import DIR
from goldbot.models import Tick

log = logging.getLogger("tracker")


class TradeTracker:
    def __init__(self, journal, notify: Callable[[str], None], cfg, clock: Callable[[], float] = time.time):
        self.j = journal
        self.notify = notify
        self.partial = float(cfg.get("tracking.tp1_partial_pct", 0.5))
        self.max_minutes = float(cfg.get("tracking.max_trade_minutes", 1440))
        self.clock = clock
        self._cache = []
        self._cache_ts = 0.0

    def _open(self):
        now = self.clock()
        if now - self._cache_ts > 1.0:        # refresh at most 1x/sec
            self._cache = self.j.open_trades()
            self._cache_ts = now
        return self._cache

    def invalidate(self):
        self._cache_ts = 0.0

    def _r_tp1_be(self, t):
        return self.partial * t["rr1"]

    def _r_tp2(self, t):
        return self.partial * t["rr1"] + (1 - self.partial) * t["rr2"]

    def on_tick(self, tick: Tick):
        if not tick.has_quote:
            return
        for t in self._open():
            self._eval(t, tick.bid, tick.ask, tick.ts_ms)

    def _eval(self, t, bid: float, ask: float, ts_ms: int):
        buy = t["direction"] == "BUY"
        px = bid if buy else ask             # price at which a position would actually be closed
        sid = t["id"]
        st = self.j.get(sid)["status"]       # fresh status (cache may be 1s old)
        if st == "CLOSED":
            return
        hit = (lambda lvl: px >= lvl) if buy else (lambda lvl: px <= lvl)
        hit_sl = (lambda lvl: px <= lvl) if buy else (lambda lvl: px >= lvl)
        now_iso = pd.Timestamp(ts_ms, unit="ms", tz="UTC").isoformat()
        dur = (pd.Timestamp(ts_ms, unit="ms", tz="UTC") - pd.Timestamp(t["timestamp"])).total_seconds() / 60.0
        sl_cur = self.j.get(sid)["sl_current"]
        if st == "OPEN":
            if hit_sl(sl_cur):                                  # conservative: SL checked before TP
                if self.j.transition(sid, "OPEN", status="CLOSED", result="SL", r=-1.0, duration_min=dur, closed_ts=now_iso):
                    self.notify(f"❌ ضرب وقف الخسارة (SL) — الذهب\n{DIR[t['direction']]} #{sid}  الدخول {t['entry']:.2f}  وقف الخسارة {t['sl']:.2f}\nالنتيجة: -1.00R")
                    self._on_close(sid)
                return
            if hit(t["tp1"]):
                if self.j.transition(sid, "OPEN", status="TP1", sl_current=t["entry"], tp1_ts=now_iso):
                    self.notify(f"✅ تحقق الهدف الأول (TP1) — الذهب\n{DIR[t['direction']]} #{sid}  الهدف {t['tp1']:.2f}\nتم نقل وقف الخسارة إلى نقطة الدخول Break Even ({t['entry']:.2f})")
                    self.invalidate()
                st = "TP1"
        if st == "TP1":
            if hit(t["tp2"]):
                if self.j.transition(sid, "TP1", status="CLOSED", result="TP2", r=self._r_tp2(t), duration_min=dur, closed_ts=now_iso):
                    self.notify(f"✅ تحقق الهدف الثاني (TP2) — الذهب\n{DIR[t['direction']]} #{sid}  الهدف {t['tp2']:.2f}\nالنتيجة: +{self._r_tp2(t):.2f}R  (أُغلقت الصفقة)")
                    self._on_close(sid)
            elif hit_sl(self.j.get(sid)["sl_current"]):
                if self.j.transition(sid, "TP1", status="CLOSED", result="TP1_BE", r=self._r_tp1_be(t), duration_min=dur, closed_ts=now_iso):
                    self.notify(f"🟡 خروج عند نقطة التعادل — الذهب\n{DIR[t['direction']]} #{sid}  أُغلقت عند الدخول بعد تحقق الهدف الأول\nالنتيجة: +{self._r_tp1_be(t):.2f}R")
                    self._on_close(sid)

    def check_expiry(self, now: pd.Timestamp, bid: float, ask: float):
        for t in self.j.open_trades():
            age = (now - pd.Timestamp(t["timestamp"])).total_seconds() / 60.0
            if age < self.max_minutes:
                continue
            buy = t["direction"] == "BUY"
            px = bid if buy else ask
            risk = abs(t["entry"] - t["sl"])
            move = (px - t["entry"]) if buy else (t["entry"] - px)
            if t["status"] == "OPEN":
                r = move / risk
            else:
                r = self.partial * t["rr1"] + (1 - self.partial) * move / risk
            if self.j.transition(t["id"], t["status"], status="CLOSED", result="EXPIRED", r=r, duration_min=age, closed_ts=now.isoformat()):
                self.notify(f"⏱ انتهت مدة الصفقة #{t['id']} بعد {age / 60:.0f} ساعة  النتيجة: {r:+.2f}R")
                self._on_close(t["id"])

    def _on_close(self, sid: int):
        """Closed live trade becomes part of the historical sample used by the probability engine."""
        import json
        t = self.j.get(sid)
        fp = json.loads(t["fingerprint"] or "{}")
        if fp:
            fp.update(ts=t["closed_ts"], tf="live", r=t["r"], outcome=t["result"], score=t["score"], duration_min=t["duration_min"])
            try:
                self.j.add_history([fp], "live")
            except Exception:
                log.exception("تعذّر حفظ الصفقة المغلقة في السجل التاريخي")
        self.invalidate()

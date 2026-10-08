"""التهيئة الذاتية: بناء السجل التاريخي (Backtest) ثم تدريب نموذج التعلم الآلي وتفعيله تلقائيًا إن اجتاز الاختبار.
يعمل تلقائيًا عند أول تشغيل (وكل N يوم) في عملية فرعية منخفضة الأولوية حتى لا يؤثر على السعر اللحظي.
بدون هذا السجل لا يوجد «احتمال تاريخي» فيمتنع البوت عن الإشارات (probability.block_if_insufficient)."""
from __future__ import annotations
import asyncio, json, logging, os, sys, time
from typing import Awaitable, Callable, Optional, Tuple
import pandas as pd

log = logging.getLogger("bootstrap")


async def _default_runner(cmd: list, log_path: str, timeout: float) -> Tuple[int, str]:
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    with open(log_path, "ab") as lf:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=lf, stderr=asyncio.subprocess.STDOUT,
                                                    preexec_fn=(lambda: os.nice(10)) if hasattr(os, "nice") else None)
        try:
            await asyncio.wait_for(proc.wait(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return -9, "انتهت المهلة"
    try:
        tail = open(log_path, "rb").read()[-600:].decode("utf-8", "ignore")
    except OSError:
        tail = ""
    return proc.returncode or 0, tail


class Bootstrapper:
    def __init__(self, app, runner: Optional[Callable[..., Awaitable]] = None):
        self.app = app
        self.cfg = app.cfg
        self.runner = runner or _default_runner
        self.marker = self.cfg.get("bootstrap.marker", "data/bootstrap.json")
        self.running = False
        self.last_summary = "لم يبدأ بعد"

    # ---------------------------------------------------------------- حالة
    def state(self) -> dict:
        try:
            with open(self.marker, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def backtest_trades(self) -> int:
        h = self.app.journal.history_trades()
        return 0 if h.empty else int((h["source"] == "backtest").sum())

    def needed(self) -> Tuple[bool, str]:
        if not self.cfg.get("bootstrap.enabled", True):
            return False, "معطّل في الإعدادات"
        n = self.backtest_trades()
        if n < int(self.cfg.get("bootstrap.min_trades", 150)):
            return True, f"السجل التاريخي قليل ({n} صفقة)"
        st = self.state()
        if not st:
            return True, "لا يوجد سجل تهيئة"
        age = (pd.Timestamp.now(tz="UTC") - pd.Timestamp(st["ts"])).days
        if age >= int(self.cfg.get("bootstrap.refresh_days", 7)):
            return True, f"آخر تحديث قبل {age} يوم"
        return False, "السجل حديث"

    async def maybe_run(self):
        need, why = self.needed()
        if need:
            log.info("بدء التهيئة الذاتية: %s", why)
            await self.run(force=True)

    # ---------------------------------------------------------------- التنفيذ
    async def run(self, force: bool = False):
        if self.running:
            return False
        self.running = True
        notify = self.app.notify_q.put_nowait
        try:
            days = int(self.cfg.get("bootstrap.days", 45))
            tfs = list(self.cfg.get("bootstrap.tfs", ["5m", "15m"]))
            cfg_path = self.cfg.get("_config_path", "config/config.yaml")
            notify(f"🧠 بدأت التهيئة الذاتية: أبني السجل التاريخي من آخر {days} يومًا ({', '.join(tfs)}) ثم أدرّب نموذج التعلم الآلي.\n"
                   "تستغرق 5–20 دقيقة وتعمل في الخلفية؛ السعر اللحظي والأوامر تعمل كالمعتاد.")
            log_path = os.path.join(self.cfg.get("paths.log_dir", "logs"), "bootstrap.log")
            for tf in tfs:
                t0 = time.time()
                rc, tail = await self.runner([sys.executable, "-m", "goldbot", "backtest", "--tf", tf, "--days", str(days), "--store",
                                              "--config", cfg_path], log_path, float(self.cfg.get("bootstrap.timeout_sec", 5400)))
                if rc != 0:
                    msg = f"⚠️ فشل بناء السجل لفريم {tf} (رمز {rc}). التفاصيل في logs/bootstrap.log"
                    self.last_summary = msg
                    notify(msg)
                    return False
                log.info("اكتمل Backtest %s خلال %.0f ثانية", tf, time.time() - t0)
            n = await asyncio.to_thread(self.backtest_trades)
            ml_txt = "التعلم الآلي: غير مفعّل في الإعدادات."
            if self.app.ml is not None:
                hist = await asyncio.to_thread(self.app.journal.history_trades)
                rep = await asyncio.to_thread(self.app.ml.train, hist)
                ml_txt = self.app.ml.describe()
            self.last_summary = f"{n} صفقة تاريخية. {ml_txt}"
            os.makedirs(os.path.dirname(self.marker) or ".", exist_ok=True)
            with open(self.marker, "w", encoding="utf-8") as f:
                json.dump({"ts": pd.Timestamp.now(tz="UTC").isoformat(), "trades": n, "ml": (self.app.ml.report if self.app.ml else {})},
                          f, ensure_ascii=False, default=str)
            notify(f"✅ اكتملت التهيئة الذاتية.\nالسجل التاريخي: {n} صفقة.\n{ml_txt}\n"
                   + ("الآن صار «الاحتمال التاريخي» متوفرًا، وستظهر الإشارات عند اكتمال الشروط." if n else
                      "تنبيه: لم تُنتج الاختبارات أي صفقة، قد تحتاج بيانات أكثر."))
            return True
        except Exception as e:
            log.exception("فشلت التهيئة الذاتية")
            notify(f"⚠️ فشلت التهيئة الذاتية: {type(e).__name__}: {e}")
            return False
        finally:
            self.running = False

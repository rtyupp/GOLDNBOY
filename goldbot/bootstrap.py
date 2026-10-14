"""التهيئة الذاتية: بناء السجل التاريخي (Backtest) الذي يُستخدم لحساب «الاحتمال التاريخي» لكل إعداد.
يعمل تلقائيًا عند أول تشغيل (وكل N يوم) في عملية فرعية منخفضة الأولوية حتى لا يؤثر على السعر اللحظي.
بدون هذا السجل لا يوجد «احتمال تاريخي» فيمتنع البوت عن الإشارات (probability.block_if_insufficient)."""
from __future__ import annotations
import asyncio, json, logging, os, sys, time
from typing import Awaitable, Callable, Optional, Tuple
import pandas as pd

from goldbot import resources

log = logging.getLogger("bootstrap")


MEM_KILL = -137          # رمز: أُوقفت العملية حمايةً للخدمة من نفاد الذاكرة


async def _default_runner(cmd: list, log_path: str, timeout: float, mem_kill_frac: float = 0.85) -> Tuple[int, str]:
    """يشغّل العملية الفرعية ويراقب ذاكرة الحاوية كل 5 ثوانٍ؛ إن تجاوزت mem_kill_frac من الحد توقفها بدل أن تقتل Render الخدمة كلها."""
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    env = dict(os.environ, MALLOC_ARENA_MAX="2")
    with open(log_path, "ab") as lf:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=lf, stderr=asyncio.subprocess.STDOUT, env=env,
                                                    preexec_fn=(lambda: os.nice(10)) if hasattr(os, "nice") else None)
        deadline = time.time() + timeout
        waiter = asyncio.ensure_future(proc.wait())
        while True:
            done, _ = await asyncio.wait({waiter}, timeout=5)
            if done:
                break
            pr = resources.pressure()
            if pr is not None and pr >= mem_kill_frac:
                proc.kill()
                await proc.wait()
                return MEM_KILL, f"الذاكرة {pr:.0%} من الحد"
            if time.time() > deadline:
                proc.kill()
                await proc.wait()
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
            plan = resources.plan_bootstrap(self.cfg)
            if not plan["run"]:
                self.last_summary = f"تأجّلت التهيئة: {plan['why']}"
                notify(f"ℹ️ {self.last_summary}. البوت يعمل بشكل طبيعي بدونها، والاحتمال التاريخي يُكتب «غير كافٍ».")
                return False
            days, tfs = plan["days"], plan["tfs"]
            cfg_path = self.cfg.get("_config_path", "config/config.yaml")
            notify(f"🧠 بدأت التهيئة الذاتية ({plan['why']}): أبني السجل من آخر {days} يومًا ({', '.join(tfs)}).\n"
                   "تعمل في الخلفية وأراقب الذاكرة؛ السعر اللحظي والأوامر تعمل كالمعتاد.")
            log_path = os.path.join(self.cfg.get("paths.log_dir", "logs"), "bootstrap.log")
            failures = []
            for tf in tfs:
                t0 = time.time()
                rc, tail = await self.runner([sys.executable, "-m", "goldbot", "backtest", "--tf", tf, "--days", str(days), "--store",
                                              "--config", cfg_path], log_path, float(self.cfg.get("bootstrap.timeout_sec", 5400)))
                if rc == MEM_KILL:
                    msg = (f"⚠️ أوقفت التهيئة حمايةً للخدمة ({tail}). البوت يعمل بدونها. "
                           "لتفعيلها: قلّل bootstrap.low_mem_days أو ارفع خطة Render.")
                    self.last_summary = msg
                    notify(msg)
                    return False
                if rc != 0:
                    # A single frame failing must not discard the frames that already succeeded
                    # (the 5m sample is written before 15m runs). Record and continue; only a total
                    # wipeout below min_trades is reported as a hard failure.
                    detail = (tail or "").strip().splitlines()[-4:]
                    detail = " | ".join(detail)[-400:]
                    failures.append(f"{tf} (رمز {rc})" + (f": {detail}" if detail else ""))
                    log.warning("فشل Backtest %s (رمز %s): %s", tf, rc, detail or "-")
                    continue
                log.info("اكتمل Backtest %s خلال %.0f ثانية", tf, time.time() - t0)
            n = await asyncio.to_thread(self.backtest_trades)
            if failures and n < int(self.cfg.get("bootstrap.min_trades", 150)):
                msg = "⚠️ فشل بناء السجل: " + " ؛ ".join(failures)
                self.last_summary = msg
                notify(msg)
                return False
            self.last_summary = f"{n} صفقة تاريخية."
            os.makedirs(os.path.dirname(self.marker) or ".", exist_ok=True)
            with open(self.marker, "w", encoding="utf-8") as f:
                json.dump({"ts": pd.Timestamp.now(tz="UTC").isoformat(), "trades": n}, f, ensure_ascii=False, default=str)
            notify(f"✅ اكتملت التهيئة الذاتية.\nالسجل التاريخي: {n} صفقة.\n"
                   + ("الآن صار «الاحتمال التاريخي» متوفرًا، وستظهر الإشارات عند اكتمال الشروط." if n else
                      "تنبيه: لم تُنتج الاختبارات أي صفقة، قد تحتاج بيانات أكثر."))
            return True
        except Exception as e:
            log.exception("فشلت التهيئة الذاتية")
            notify(f"⚠️ فشلت التهيئة الذاتية: {type(e).__name__}: {e}")
            return False
        finally:
            self.running = False

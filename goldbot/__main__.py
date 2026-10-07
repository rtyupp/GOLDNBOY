"""الأوامر:  python -m goldbot run | backtest | train-ml | check"""
from __future__ import annotations
import argparse, asyncio, json, os, signal, sys
import pandas as pd
from goldbot.config import load_config, env
from goldbot.monitor import setup_logging
from goldbot import i18n as T


def build_app(cfg):
    from goldbot.app import BotApp
    from goldbot.notify.telegram import Telegram
    from goldbot.providers.factory import make_history_provider
    tok, ch = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHANNEL_ID")
    admins = [int(x) for x in (env("TELEGRAM_ADMIN_IDS", "") or "").replace(" ", "").split(",") if x]
    target = cfg.get("telegram.signal_target", "private")
    need_ok = bool(admins if target in ("private", "both") else True) and bool(ch if target in ("channel", "both") else True)
    tg = Telegram(tok, ch, admins, target) if (tok and need_ok) else None
    if tg is None and cfg.get("mode") != "paper":
        print("تنبيه: متغيرات تيليجرام ناقصة (TELEGRAM_BOT_TOKEN و TELEGRAM_ADMIN_IDS للخاص، أو TELEGRAM_CHANNEL_ID للقناة) "
              "← سيعمل البوت في الوضع التجريبي بدون إرسال", file=sys.stderr)
        cfg.set("mode", "paper")
    return BotApp(cfg, tg=tg, history_provider=make_history_provider(cfg))


def cmd_run(cfg):
    app = build_app(cfg)

    async def runner():
        loop = asyncio.get_running_loop()
        for s in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(s, app.stop)
            except NotImplementedError:
                pass
        await app.run()
    asyncio.run(runner())


def _history_1m(cfg, days):
    from goldbot.providers.factory import make_history_provider
    from goldbot.core.history_store import load_cached
    return load_cached(make_history_provider(cfg), cfg.get("symbol", "XAUUSD"), "1m", days, cfg.get("data.cache_dir", "data/cache"))


def cmd_backtest(cfg, a):
    from goldbot.backtest.engine import run_backtest, save_report, store_probability_history
    from goldbot.tracking.journal import Journal
    days = a.days or int(cfg.get("backtest.history_days", 60))
    base = _history_1m(cfg, days)
    print(f"البيانات التاريخية: {len(base)} شمعة 1m  من {base.index[0]} إلى {base.index[-1]}")
    j = Journal(cfg.get("paths.journal", "data/journal.sqlite"))
    tfs = ["1m", "5m", "15m"] if a.tf == "all" else [a.tf]
    for tf in tfs:
        step = a.step or int(cfg.get(f"backtest.steps.{tf}", 1))
        res = run_backtest(base, cfg, tf, step=step, progress=lambda k, n: print(f"  {tf}: {k}/{n}", flush=True))
        p = save_report(res, cfg.get("backtest.report_dir", "reports"))
        print(f"[{tf}] تم حفظ التقرير: {p}")
        for n, r in res["strategies"].items():
            print(f"  {T.STRATEGY.get(n, n):18s} الصفقات={r.get('total_trades', 0):4d} نسبة الربح={r.get('win_rate')} "
                  f"متوسط R={r.get('avg_r')} معامل الربح={r.get('profit_factor')} أقصى تراجع(R)={r.get('max_drawdown_r')}")
        if a.store:
            print(f"  تم حفظ {store_probability_history(j, res)} صفقة في سجل الاحتمال التاريخي")
    print("ملاحظة: نتائج الـ Backtest تقدير للماضي وليست ضمانًا للمستقبل. السبريد مفترض ولم يُطبَّق فلتر الأخبار.")


def cmd_train_ml(cfg):
    from goldbot.ml.model import MLGate
    from goldbot.tracking.journal import Journal
    j = Journal(cfg.get("paths.journal", "data/journal.sqlite"))
    rep = MLGate(cfg).train(j.history_trades())
    print(json.dumps(rep, indent=2, default=str, ensure_ascii=False))
    print("فعّل ml.enabled: true في config.yaml فقط إذا كانت passed=true.")


def cmd_check(cfg):
    import requests
    ok = lambda v: "موجود ✅" if v else "ناقص ❌"
    print("SIFTING_API_KEY      :", ok(env("SIFTING_API_KEY")))
    print("TELEGRAM_BOT_TOKEN   :", ok(env("TELEGRAM_BOT_TOKEN")))
    print("TELEGRAM_ADMIN_IDS   :", env("TELEGRAM_ADMIN_IDS") or "ناقص ❌  (اكتب /id للبوت ليعطيك رقمك)")
    print("وجهة الإشارات        :", cfg.get("telegram.signal_target", "private"), "| TELEGRAM_CHANNEL_ID:", env("TELEGRAM_CHANNEL_ID") or "غير مستخدم الآن")
    model = env("GEMINI_MODEL") or cfg.get("ai.model") or "gemini-3.8-flash"
    print("Gemini               :", ("دالتك الحالية: " + cfg.get("ai.adapter")) if cfg.get("ai.adapter")
          else f"REST — النموذج {model} — GEMINI_API_KEY {ok(env('GEMINI_API_KEY'))}")
    print("Gemini fallback      :", env("GEMINI_FALLBACK_MODEL") or cfg.get("ai.fallback_model") or "غير محدد")
    if not cfg.get("ai.adapter") and "2.5" in model:
        print("  ⚠️ نماذج Gemini 2.5 مجدولة للإيقاف في 16 أكتوبر 2026؛ حدّد ai.fallback_model أو نموذجًا أحدث من توثيق Google.")
    if env("TELEGRAM_BOT_TOKEN"):
        r = requests.get(f"https://api.telegram.org/bot{env('TELEGRAM_BOT_TOKEN')}/getMe", timeout=15).json()
        print("اتصال تيليجرام       :", ("نجح ✅ @" + r["result"]["username"]) if r.get("ok") else r)
    if env("SIFTING_API_KEY"):
        from goldbot.providers.factory import make_history_provider
        end = pd.Timestamp.now(tz="UTC")
        df = make_history_provider(cfg).fetch("1m", end - pd.Timedelta(hours=3), end)
        print("SiftingIO REST       :", f"نجح ✅ ({len(df)} شمعة)" if len(df) else "لا توجد شموع (السوق مغلق؟)")
    from goldbot.analysis.news import NewsFilter
    print("ملف الأخبار          :", len(NewsFilter(cfg.get("news.events_file")).events), "حدث (املأ data/events.csv قبل التشغيل الحقيقي)")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="goldbot", description="بوت إشارات الذهب XAU/USD")
    ap.add_argument("command", choices=["run", "backtest", "train-ml", "check"])
    ap.add_argument("--config", default=os.environ.get("GOLDBOT_CONFIG", "config/config.yaml"))
    ap.add_argument("--tf", default="5m", choices=["1m", "5m", "15m", "all"])
    ap.add_argument("--days", type=int)
    ap.add_argument("--step", type=int)
    ap.add_argument("--store", action="store_true", help="حفظ صفقات الـ Backtest كسجل للاحتمال التاريخي")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    setup_logging(cfg.get("log_level", "INFO"), cfg.get("paths.log_dir", "logs"))
    {"run": lambda: cmd_run(cfg), "backtest": lambda: cmd_backtest(cfg, a),
     "train-ml": lambda: cmd_train_ml(cfg), "check": lambda: cmd_check(cfg)}[a.command]()


if __name__ == "__main__":
    main()

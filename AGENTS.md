# AGENTS.md — GOLDNBOY (XAU/USD signal bot)

## Environment
- Python deps are NOT preinstalled in a fresh session: run `pip install -r requirements.txt`
  (pandas, numpy, requests, PyYAML, matplotlib, ta). `ruff` also needs `pip install ruff`.
- Run a quick live sanity check with `python -m goldbot check` (tests providers + prints mode).
- Full suite: `python -m unittest discover -s tests` (~2 min, 203 tests, 1 skipped).
- Lint gate used by this repo: `python -m ruff check goldbot/ tests/ --select F,E9`.
- Scripts that import `goldbot` need `PYTHONPATH=/workspace/GOLDNBOY` when run from `/tmp`.

## Diagnostics
- `python -m goldbot run` defaults to `mode: paper` (logs only, no Telegram). Real sending needs
  `BOT_MODE=live`. This is the #1 reason "no signals arrive" on a fresh deploy.
- To reproduce a decision offline: load history via
  `goldbot.core.history_store.load_cached(...)`, `MarketData.load_history(df)`, then
  `Pipeline.prepare(frames, bid, ask, as_of, True, "OK")` and print `cand.log.block()`.
  This shows the exact gate (`reasons`, `global_reasons`) that blocked a signal.
- Common blockers: `HTF_CONFLICT(4H vs 1H)`, `NO_SETUP`, `LOW_CONFLUENCE`, `SL_ILLOGICAL`
  (invalidation on the wrong side of entry — a legitimate risk rejection).

## Config / profiles
- `load_config(path, overrides, profile)` merges `config/config.yaml` with `config/<profile>.yaml`;
  `GOLDBOT_PROFILE` env or `--profile` selects it. `scalp` profile = fast 15m scalping.
- `strategies.require_htf_alignment` (default `true`): when `false`, the HTF checks become weighted
  (not required). `confluence.ignore_htf_conflict` (default `false`): when `true`, drop the
  `HTF_CONFLICT` global gate. The scalp profile sets both to relax, because 15m scalps are short.

## Strategies (11) — `goldbot/strategies/`
- Classic 5 in `impl.py`; SMC 2 in `smc.py`; ORB + Round Number in `impl.py`; advanced 2 in `advanced.py`.
- `advanced.py` adds `LiquidityGrab` (PDH/PDL sweep + reclaim) and `SmcSilverBullet` (ICT killzone
  window + OTE + FVG + MSS). Both reuse existing helpers (`impulse_leg`, `ote_band`, `in_ote`,
  `_confirm_candle`, `_overlap` from `smc.py`) — do NOT duplicate that logic.
- Register a strategy in `impl.py::ALL` (append) AND in `config/config.yaml` `strategies.enabled`,
  or it never runs. Keep i18n `T.STRATEGY` + `T.CHECKS` in sync.
- Silver Bullet windows: `strategies.silver_bullet.windows` (UTC, no DST auto-adjust).

## Filters / gates
- `filters.block_regimes` (default `[]`): global gate `REGIME_BLOCK(<1H trend>)` in
  `engine/no_trade.py::global_gates`. Do NOT block `TRANSITION`: it is ~71% of the time on XAUUSD
  (1H trend), and blocking it nearly silenced the bot — over 4 days tradable signals fell 46 -> 7,
  i.e. back to the losing pre-fix count. It is a per-regime kill switch, not a quality lever.
- `confluence.min_score` (default `55.0`): minimum confluence for a signal. Was `68.4`, which on 28d
  real XAUUSD selected only losers (5m n=11 avgR -0.52, -5.7R). At `55` with NO regime block:
  5m n=29 avgR -0.03 and 15m n=15 avgR +0.10 (net positive, ~5x the trade count). The scalp profile
  overrides it to `50`. Score is `S×100/95` (news check removed). This is the correct lever.
- Capital protection (default OFF, `signals.max_daily_loss_r=0`, `signals.max_consecutive_losses=0`):
  `engine/no_trade.py::capital_gates` runs in `BotApp._apply_limits` only after a candidate is
  already tradable, so empty cycles are not tinted. `DAILY_LOSS_LIMIT(-X R)` sums realized R of
  trades CLOSED since UTC midnight (`Journal.realized_r_since`); `LOSS_STREAK(N)` counts trailing
  closed losses (`Journal.consecutive_losses`, reset by a win). Both are advisory caps on NEW
  signals — they never touch open trades. `/stats` shows the current standing via
  `BotApp.protection_status`. Tests: `tests/test_capital.py`.
- Position sizing (display only, `risk.account_balance=0` disables): `engine/risk.py::position_size`
  turns balance × `risk_per_trade_pct` and the plan's stop distance into lots/oz for the signal
  message (gold: 1 lot = 100 oz). Never alters the decision.

## Backtesting / comparing (no fabricated edge)
- `python -m goldbot compare` now runs baseline / +SMC / +advanced (with costs) / all (no costs),
  and scores EVERY new strategy (`BASELINE_KEYS`, `NEW_KEYS`, `ADVANCED_KEYS` in `backtest/compare.py`).
- Honest result: the whole ensemble is negative IS+OOS; loosening gates only adds losers.
  Only Round Number had a clear positive stat. Never present a small-sample
  improvement as proven — check the walk-forward out-of-sample slice and report trade counts.
- History window (`backtest.history_days`, `bootstrap.days`, default `58`): free Yahoo keeps only
  ~28 days of 1m but ~58 days of 5m, so a 1m-only base capped the probability sample at ~70 trades.
  `core/history_store.py::load_backtest_base` stitches **native 5m bars for the older span in front of
  native 1m bars for the recent ~28 days** (gated by `backtest.extend_with_native_5m`, default true;
  falls back to 1m-only when the source cannot serve deeper 5m). The sample roughly triples (~300 vs
  ~70) while the recent window keeps full 1m precision (trade simulation + the 1m `confirm1` flag).
  Native-5m and resampled-1m give the same trade count over the shared 28d window (48 vs 47), so the
  seam is not a fidelity problem. Bootstrap on the free 512MB tier (`resources.plan_bootstrap`): set
  `bootstrap.low_mem_days` (default `45`) **above** the 1m cap (28), or the low-memory plan clamps the
  window back to 28 and gains nothing. Dukascopy (the only free deeper-1m source) is rate-limited/
  blocked from cloud hosts, so it cannot extend the 1m cap.
- Multi-fold walk-forward: `engine/walk_forward_folds` splits the trade list into `backtest.wf_folds`
  (default 4) chronological folds and scores each on its own. `compare.verdict` now also requires
  `walk_forward_folds.consistent` (a majority of folds positive) so a strategy that only wins on one
  lucky slice is rejected — a single 70/30 split could hide that. Empty when the sample is too small
  to fold (folds are then not enforced). Reports include `walk_forward_folds` next to `walk_forward_split`.
- `/stats` breaks closed trades down by strategy, session, market regime (1H trend, read from the
  stored fingerprint) and UTC hour, so a weak regime/hour is visible without a backtest run.
- Realistic spread: `_spread_for` widens the assumed spread by session (`backtest.spread_session_mult`,
  e.g. `{"Off-hours": 3.0}`) and inside a news window (`backtest.spread_news_mult` ×
  `backtest.news_window_min`). `python -m goldbot compare` reads event times from the local calendar
  cache (no network) and passes them in. All multipliers default to none/0, so the spread stays flat
  as before until enabled. Costs block in the report records the base spread + multipliers.
- Report filenames are sanitized (`save_report`): strategy labels can contain `/`
  (`Liquidity Grab (PDH/PDL)`), and an unsanitized label made pandas write into a non-existent
  subdirectory — an `OSError` that aborted the whole bootstrap subprocess with exit code 1. This was
  data-dependent (it only crashed when that strategy produced ≥1 trade in the frame), so 5m passed
  while 15m failed. `bootstrap.run` now also continues past a single frame failure (keeping the frames
  that already stored trades) and surfaces the subprocess error tail in the notification, instead of
  pointing at `logs/bootstrap.log`, which is not reachable from the Render dashboard.
- Adaptive confluence threshold (`confluence.adaptive_*`, off by default): when the recent average R
  over the last `adaptive_lookback` closed trades is negative (and at least `adaptive_min_samples`
  exist), `no_trade.adaptive_min_score` raises the effective `min_score` by `adaptive_step` (capped by
  `adaptive_max_bump`), so only high-quality setups pass instead of a hard trading halt. The
  `LOW_CONFLUENCE(...)` reason shows the raised threshold. Regression: `tests/test_adaptive_and_spread.py`.

## Add-ons (guardrails & persistence)
- News filter: `goldbot/providers/news.py` (free ForexFactory weekly JSON, no key). Wired into
  `Pipeline(cfg, journal, news=...)`; blocks via `NEWS_BLACKOUT(...)` before strategies run.
  Fail-open: unreachable calendar blocks nothing. Command `/news`. Config `news.*`.
- Journal persistence: `goldbot/persist.py` syncs `journal.sqlite` to a PRIVATE gist using
  `GITHUB_TOKEN`; restores at startup only when the local journal has zero rows. Config
  `persistence.*`. Fail-open. Verified by `tests/test_addons.py`.
  - RESTORE MUST NOT OVERWRITE THE DB UNDER A LIVE CONNECTION. The app opens the Journal
    (WAL mode) before `restore()`. Writing the file in place leaves stale `-wal`/`-shm`
    sidecars that SQLite replays over the restored data, wiping it (record looks "corrupt"
    while the source is fine). `restore()` closes the connection (`Journal.close_connection`),
    removes the sidecars, writes, then `Journal.reopen()`s. Regression:
    `test_restore_replaces_file_under_a_live_wal_connection`.
- News calendar URL: ForexFactory publishes only the CURRENT week
  (`ff_calendar_thisweek.json`). The old `ff_calendar_nextweek.*` endpoint is a permanent 404 —
  never point config/defaults at it; `NewsCalendar` ignores non-`http(s)` URL values.
- ForexFactory also answers `429 Too Many Requests` transiently (Cloudflare). A 429 is NOT a hard
  failure: `refresh()` backs off (`_next_try`, honouring `Retry-After`, 60–1800s), keeps the last
  good calendar, and does not log a scary error on every refresh. The backoff applies even to
  `force=True` (so the `/news` command cannot hammer the host). Regression:
  `test_throttled_refresh_keeps_previous_calendar`.
- The repo once forbade ANY news reference (`tests/test_ui_and_removal.py`). That guard now only
  forbids AI/ML decision-making; the news filter is an intentional, user-approved exception.

## Strategies / risk
- `LiquiditySweep` invalidation is the sweep wick extreme. If price has already moved past that
  extreme the setup is STALE: its stop lands on the wrong side of entry, so `RiskEngine` rejects it
  as `SL_ILLOGICAL` — yet the checks alone scored it 100 in the field. The strategy now requires
  `price still beyond sweep invalidation` (price on the correct side of the extreme) as a hard
  check, so phantom "perfect" setups never surface. Measured 109/720 (≈15%) of 5m/15m sweep
  candidates were stale before the fix. Regression: `tests/test_advanced.py::TestStaleSweepGuard`.
  - Trade-off: the sweep selection still prefers the most recent candidate (not the best-sided
    one), so some cycles now correctly report "no sweep setup" instead of a stale one. Fixing the
    candidate selection to prefer a valid-sided sweep is a possible future refinement.

## Shutdown / resilience
- Cold start: the analysis loop must not run before the initial history load. Otherwise the
  first cycle at every deploy records a misleading `INSUFFICIENT_HISTORY(0<500)` (0 rows
  because `load_history` has not returned yet), which looks like a broken data feed. `BotApp`
  sets `_history_attempted` once the **first** load attempt finishes (success *or* failure) and
  `_analysis_tick` defers the cycle only while it is still in flight. Gating on "ready" instead
  would freeze the analysis forever during a real feed outage, hiding it from cycles and `/why`.
  `_history_ready` is set only on a successful load. Regression:
  `tests/test_review_regressions.py::TestStartupHistoryGate`.
- Honest `/health`: the endpoint used to hard-code `"ok": true`, so a stopped history provider or a
  stale tick feed still reported healthy. `BotApp.health_snapshot()` now returns `ok=false` when the
  live tick is stale/disconnected or when deep history is stale (`HISTORY_STALE`, see
  `deep_history_status` + `data.max_history_staleness_min`, default 180 min, skipped while the market
  is closed). A closed market is explicitly `ok=true` (not an outage). The HTTP status stays 200 on
  purpose — returning 503 would make Render restart a bot that is merely honestly reporting an
  outage. `/health` (command + button) prints the same snapshot.
- Deep-history staleness guard: `deep_history_status` flags `HISTORY_STALE` when the newest 1m bar is
  older than `data.max_history_staleness_min` outside market hours. A provider that stops returning
  new bars keeps the row count above `min_history_rows`, so it otherwise hides inside
  `INSUFFICIENT_HISTORY`. Surfaced in `/status` and the health snapshot.
- Weekend gap in trade expiry: `TradeTracker.check_expiry` now ages trades by **trading minutes**
  (`_trading_minutes`), excluding the weekly close (Fri 21:00 → Sun 22:00 UTC). Before, a trade
  opened before the weekend "expired" on Sunday merely because the wall clock advanced while the
  market never moved. Regression: `tests/test_health_and_expiry.py`.
- SIGTERM must terminate the process. The `/health` server used to `await srv.serve_forever()`
  with no stop check, so `asyncio.gather(*tasks)` in `run()` never returned and Render SIGKILLed
  the container after every deploy (no clean stop, no final journal push). `BotApp` now stores
  `self._health_srv`, closes it in `stop()`, cancels tasks, and `run()` awaits the cancelled
  tasks. Regression: `tests/test_review_regressions.py::TestGracefulShutdown`.
- Strategies are evaluated in isolation: `Pipeline._eval` wraps `strategy.evaluate` and turns any
  exception into a `NONE` result with `reason="strategy error"`. One bad strategy (or a degenerate
  ATR) must not abort the whole minute's analysis / signal path. Regression:
  `TestStrategyIsolation::test_crashing_strategy_becomes_none_and_others_run`.
- `Analyzer.tf_state` rejects `atr <= 0` as well as NaN (`not (atr > 0)`). A zero/negative ATR
  previously reached `RiskEngine.build` and raised `ZeroDivisionError`. Regression:
  `test_nonpositive_atr_is_rejected_not_divided_by`.
- News JSON is untrusted: `_parse_events` skips non-dict array entries (nulls, nested lists,
  strings from a CDN error page) instead of raising `AttributeError` and discarding the calendar.

## Git / delivery
- Default remote: https://github.com/rtyupp/GOLDNBOY.git.
- The injected `$GITHUB_TOKEN` is READ-ONLY (403 on push). Pushing requires the user's PAT,
  supplied inline by the user in-session. Never persist tokens in `.git/config` (verify with
  `grep -c github_pat .git/config` == 0 after any push).
- Workflow: branch off `origin/main` (merged branches are not reused), commit with
  `Co-authored-by: openhands <openhands@all-hands.dev>`, push, open PR, then merge to main.

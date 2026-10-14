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
  `engine/no_trade.py::global_gates`. Evidence (28d, no confluence gate): TRANSITION avgR -0.34,
  BEARISH -0.36 vs BULLISH +0.30, RANGE +0.34. Strongest lever found, but small sample → off by default.

## Backtesting / comparing (no fabricated edge)
- `python -m goldbot compare` now runs baseline / +SMC / +advanced (with costs) / all (no costs),
  and scores EVERY new strategy (`BASELINE_KEYS`, `NEW_KEYS`, `ADVANCED_KEYS` in `backtest/compare.py`).
- Honest result on 28d: the whole ensemble is negative IS+OOS; loosening gates only adds losers.
  Only Round Number had a clear positive stat (n=15, avgR +0.40). Never present a small-sample
  improvement as proven — check the walk-forward out-of-sample slice and report trade counts.

## Add-ons (guardrails & persistence)
- News filter: `goldbot/providers/news.py` (free ForexFactory weekly JSON, no key). Wired into
  `Pipeline(cfg, journal, news=...)`; blocks via `NEWS_BLACKOUT(...)` before strategies run.
  Fail-open: unreachable calendar blocks nothing. Command `/news`. Config `news.*`.
- Near-miss alert: `BotApp._maybe_near_miss` in `app.py` (only when `mode=live`). Fires when the
  best setup score >= `near_miss.score` but the cycle was blocked; per-setup cooldown.
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

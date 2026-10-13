# AGENTS.md — GOLDNBOY (XAU/USD signal bot)

## Environment
- Python deps are NOT preinstalled in a fresh session: run `pip install -r requirements.txt`
  (pandas, numpy, requests, PyYAML, matplotlib, ta). `ruff` also needs `pip install ruff`.
- Run a quick live sanity check with `python -m goldbot check` (tests providers + prints mode).
- Full suite: `python -m unittest discover -s tests` (~2 min, 182 tests, 1 skipped).
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

## Add-ons (guardrails & persistence)
- News filter: `goldbot/providers/news.py` (free ForexFactory weekly JSON, no key). Wired into
  `Pipeline(cfg, journal, news=...)`; blocks via `NEWS_BLACKOUT(...)` before strategies run.
  Fail-open: unreachable calendar blocks nothing. Command `/news`. Config `news.*`.
- Near-miss alert: `BotApp._maybe_near_miss` in `app.py` (only when `mode=live`). Fires when the
  best setup score >= `near_miss.score` but the cycle was blocked; per-setup cooldown.
- Journal persistence: `goldbot/persist.py` syncs `journal.sqlite` to a PRIVATE gist using
  `GITHUB_TOKEN`; restores at startup only when the local journal has zero rows. Config
  `persistence.*`. Fail-open. Verified by `tests/test_addons.py`.
- The repo once forbade ANY news reference (`tests/test_ui_and_removal.py`). That guard now only
  forbids AI/ML decision-making; the news filter is an intentional, user-approved exception.

## Git / delivery
- Default remote: https://github.com/rtyupp/GOLDNBOY.git.
- The injected `$GITHUB_TOKEN` is READ-ONLY (403 on push). Pushing requires the user's PAT,
  supplied inline by the user in-session. Never persist tokens in `.git/config` (verify with
  `grep -c github_pat .git/config` == 0 after any push).
- Workflow: branch off `origin/main` (merged branches are not reused), commit with
  `Co-authored-by: openhands <openhands@all-hands.dev>`, push, open PR, then merge to main.

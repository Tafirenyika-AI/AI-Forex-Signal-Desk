# AI Trading Desk V4 — Implementation Log

Source brief: "AI TRADING DESK V4 — FINAL MASTER IMPLEMENTATION BLUEPRINT" (pasted 2026-10-08). Companion docs: `docs/V4_ARCHITECTURE.md` (repo assessment + gap analysis + implementation plan), `docs/V4_SAFETY_AUDIT.md` (Alpaca position safety), `docs/V4_DATA_SOURCES.md` (API costs). Follows the same per-phase logging convention `docs/EQUITY_V2_IMPLEMENTATION_LOG.md` already established — see that document for the format every entry below follows (files changed, schema changes, tests, execution-impact assessment, known limitations).

**Phase numbering note**: this brief restarts its own phase numbering from 0 (Audit/safety → Market intelligence → Strategy research → Adaptive AI → Learning/performance → Dashboard/TradingView → Shadow evaluation → Controlled paper execution), distinct from the Equity V2 brief's own 20-phase numbering. Do not confuse the two in a future session — Equity V2 is fully complete and separately logged.

---

## Phase 0 — Audit and Safety — **DONE**

2026-10-08. Produced the three required Phase 0 deliverables (`docs/V4_ARCHITECTURE.md`, `docs/V4_SAFETY_AUDIT.md`, `docs/V4_DATA_SOURCES.md`) and this log. Per the brief's own instruction, Phase 0 began with a live, read-only Alpaca safety check before anything else — which surfaced two real, currently-active bugs undermining existing position protection, fixed with the user's explicit approval (same standard as every execution-adjacent change in the Equity V2 effort).

**Real bugs found and fixed, full detail in `docs/V4_SAFETY_AUDIT.md`**:
1. `src/run_loop.py`'s `_normalized_positions()` double-negated Alpaca's already-signed position `qty` field, making both of this account's real open short positions (AAPL -400, MSFT -383) read as `"long"` — silently defeating the Equity V2 Phase 14 same-symbol pyramiding gate for exactly these two real positions.
2. `AlpacaBroker.get_equity_stop_price()` and `src/reconciliation/alpaca.py`'s `_check_protective_orders()` both queried orders with `status="open"`, which excludes Alpaca's real `"held"` status — the status a live, genuinely protective stop order carries while its sibling take-profit leg is still resting. This made the risk governor's correlation gate treat both real, protected positions as having `float('inf')` unbounded risk, and made the scheduled (every-30-minutes) reconciliation task raise a false `CRITICAL unprotected_position` finding for both, repeatedly.

Both fixed with dedicated regression tests reproducing the exact real bug patterns (`tests/test_alpaca_short_position_sign_bug.py`, `tests/test_alpaca_equity_stop_price_status_bug.py`, plus 2 new tests in `tests/test_reconciliation_alpaca.py`), live-verified against the real account after the fix, and pushed (`2f371c7`) before the rest of Phase 0's documentation work continued.

**Files changed**: `src/run_loop.py`, `src/broker/alpaca.py` (+ new module-level `ALPACA_TERMINAL_ORDER_STATUSES` constant), `src/reconciliation/alpaca.py`, 2 new test files + 1 extended test file (12 new tests total).

**Schema changes**: none.

**Tests**: 12 new, all passing. Full suite: **348/348 passing** (336 inherited from Equity V2 + 12 new).

**Execution-impact assessment**: the fixes themselves touch risk-governor-adjacent and reconciliation code (read-only broker queries + risk-calculation logic), but every change is a correctness fix to existing GET-only, non-order-placing code paths — confirmed by re-reading both files end to end. No new order-capable code was introduced. Explicitly approved by the user before being made, given the live-trading stakes.

**Gap analysis / architecture assessment**: see `docs/V4_ARCHITECTURE.md` for the full section-by-section comparison against the V4 spec. Headline finding: roughly half of the V4 brief's 18 content sections already have substantial, tested, live-verified coverage from the Equity V2 effort (most notably Section 16 "broker-verified performance," which is essentially fully covered already); the other half — the Strategy Research Laboratory (6/7), the opportunity scanner (5), TradingView (17), and the full dashboard restructure (19) — are genuinely new work.

**API costs**: see `docs/V4_DATA_SOURCES.md`. No new paid service is required to begin Phases 1–6; the two real paid options found (Alpaca's $99/mo SIP+corporate-actions tier, TradingView's $12.95+/mo webhook-capable plans) are each tied to a specific, deferrable ask and should wait for an explicit decision closer to when they're actually needed.

**Known limitations, disclosed not hidden**:
- `LEGACY_OPEN_POSITION` is still not built (flagged since Equity V2's own Phase 0 audit, still open) — the single largest remaining structural gap before any new V4 strategy/scanner/meta-model code should be allowed to run even in shadow mode against a real account that might later gain real positions.
- The 5 `V4_*` env-var feature flags named in Section 2 do not exist yet — this project's existing convention is CLI flags, not env-var flags; the new convention needs to be added, not retrofitted onto the old one.
- The 8 real `UNRESOLVED unexplained_broker_order` findings surfaced live during this audit's own reconciliation verification were not investigated further in this pass (same pre-existing, already-known category Equity V2 Phase 1 first found) — a candidate for a closer look under a future Section 16 extension, not resolved here.

**Next**: awaiting direction on which Priority from `docs/V4_ARCHITECTURE.md`'s implementation plan to pick up first — Priority 1 (the `LEGACY_OPEN_POSITION` / feature-flag structural gap) is the recommended starting point given it blocks safely shadow-testing anything else, but every Priority requires its own explicit go-ahead before implementation begins, per the brief's own "do not change existing execution behavior... without explicit approval" instruction.

---

## Priority 1 — `LEGACY_OPEN_POSITION` Classification & V4 Feature Flags — **DONE**

2026-10-08 (user said "go on"). Builds the structural foundation Section 2 requires before any new V4 strategy/scanner/meta-model code is allowed to run at all: the `LEGACY_OPEN_POSITION` classification mechanism and the 5 named feature flags, neither of which had any prior equivalent in this project.

**`src/v4/feature_flags.py`**: `v4_enabled`/`v4_shadow_only`/`v4_auto_promotion`/`v4_allow_new_paper_orders`/`v4_tradingview_enabled`, each a thin `os.environ.get` read, read fresh on every call (never cached, so a human can flip one in the real environment without a process restart), each defaulting to the exact safe value the brief itself names. Deliberately a NEW convention (env vars), kept separate from this project's existing CLI-argument flags (`--auto-execute`, `--enable-trailing-stops`), which gate different, already-live behavior and must never be weakenable by anything in this new set.

**`src/v4/legacy_position.py`**: `activate_v4()` takes a one-time SNAPSHOT of every symbol with a real open position at the moment of activation — not a timestamp comparison, since a real position on this system is often built from several separate fills over days with no one clean "opened_at" (confirmed directly during Phase 0's own safety audit: this account's real AAPL short was 2 tranches, MSFT was 3). Once a symbol is in the snapshot it stays legacy-protected indefinitely, a deliberately conservative choice. `is_legacy_position()` is the one function any future V4 strategy MUST check before ever proposing a trade — defaults to `True` (fully protected) for every instrument whenever V4 has never been activated at all for a given (user, broker), the safest possible starting state. `activate_v4()` refuses to run a second time for the same (user, broker), preventing a silent, accidental change to an already-committed protection boundary.

**Schema**: new additive table `v4_activation` (one row per user+broker: `activated_at` + `legacy_symbols_json` snapshot + `set_by` audit field) — confirmed created cleanly against the real production database (43→44 tables). **V4 has deliberately NOT been activated for the real account in this pass** — confirmed live (`get_activation_state(engine, 1, "alpaca").activated == False`) — activation is a genuine decision point (it commits a permanent snapshot), not an automatic consequence of building the mechanism.

**Real bug caught by this phase's own tests (not live)**: the same SQLite-vs-Postgres `DateTime(timezone=True)` round-trip gap already found and fixed twice during the Equity V2 effort (Phases 9 and 10) appeared a third time here — `get_activation_state`'s read of `activated_at` came back timezone-naive against the in-memory test engine. Fixed with the same defensive `_ensure_utc()` normalization pattern already established.

**Files changed**: `src/data/db.py` (+1 table), `src/v4/__init__.py` (new), `src/v4/feature_flags.py` (new), `src/v4/legacy_position.py` (new), `tests/test_v4_feature_flags.py` (new), `tests/test_v4_legacy_position.py` (new).

**Tests**: 50 new (43 feature-flag tests via parametrization + 7 legacy-position tests) — every flag's default value and truthy/falsy string parsing, legacy classification before/after activation, case-insensitive symbol matching, per-(user,broker) scoping, double-activation refusal (confirming the original snapshot survives untouched), timestamp/audit-field persistence, and the "activated while genuinely flat" edge case. Full suite: **398/398 passing** (348 prior + 50 new).

**Execution-impact assessment**: zero. No broker call exists anywhere in either new module; `activate_v4()` writes only to the new `v4_activation` table, never touches `orders_fills`/`trade_intents`/any execution-adjacent table, and was never called against the real production database in this pass.

**Known limitations, disclosed not hidden**:
- No call site yet actually CHECKS `is_legacy_position()` before proposing a trade — there is no new-V4 strategy code yet to wire it into (that is Priority 3's job). This phase builds the mechanism future work is required to call, not an enforcement already wired in anywhere.
- A genuine re-activation path (for a full boundary reset) is deliberately not implemented — the brief doesn't ask for one, and adding it now would be speculative; `activate_v4()`'s refusal-to-overwrite is the right default until a real need appears.
- The real account's two open positions (AAPL, MSFT) remain unclassified (V4 not yet activated) — they stay protected exactly as before, by the existing, already-live pipeline and governor, unaffected either way by this phase.

**Next**: Priority 2 (small, concrete, low-risk gaps — `BENCHMARK_INSTRUMENTS` additions, `src/models/regime.py` extensions, MFE/MAE tracking, a `rejected_signal_outcomes` table) or Priority 3 (the Strategy Research Laboratory) — continuing per "go on."

---

## Priority 2 (part 1) — Benchmark instruments + regime detail columns — **DONE**

2026-10-08, continuing per "go on." Covers the first two of Priority 2's four items from `docs/V4_ARCHITECTURE.md` (Section 4 and Section 8 gaps); MFE/MAE tracking and `rejected_signal_outcomes` are the remaining two, picked up next.

**`src/scripts/backfill_candles.py`**: `BENCHMARK_INSTRUMENTS` now also includes `IWM` (small-cap benchmark — a genuinely different regime driver than SPY's large-cap-heavy composition), `GLD`/`IAU` (gold, the standard risk-off/inflation-hedge cross-market reference), and `USO` (oil, a standalone macro driver beyond XLE's energy-sector proxy). Data-only, additive — these four will simply start accumulating history on the next scheduled backfill run, same as every existing benchmark ETF. **Live-verified**: queried Alpaca directly for all four symbols — `IWM` returned 27 real H1 bars (e.g. close 277.67 at 2026-10-07 20:00 UTC), `GLD` 23 bars (close 375.85), `IAU` 21 bars (close 77.07), `USO` 24 bars (close 143.925) — confirms all four are valid, actively-traded Alpaca symbols before relying on them.

**`src/models/regime.py`**: added `regime_direction` (`TREND_UP`/`TREND_DOWN`, split by the sign of `trend_slope`, only set when `regime == TREND`), `regime_low_volatility` (boolean, the low-percentile mirror of the existing `HIGH_VOLATILITY` check — `vol_percentile <= 0.15`, only within `RANGE`), `regime_probability` (a cheap uncertainty proxy computed from how far the deciding percentile sits past its threshold — 1.0 for `SHOCK` by construction, a real margin for `HIGH_VOLATILITY`/`TREND`/`RANGE`, 0.0 during warm-up), and `regime_event_driven` (an optional caller-supplied boolean flag, defaulting to `False` for every existing call site).

**Deliberately did NOT widen the `regime` column's own value set** (e.g. replacing `TREND` with `TREND_UP`/`TREND_DOWN` directly) — confirmed by reading `src/decision/fusion.py` that `REGIME_WEIGHT_MULTIPLIERS`/`REGIME_THRESHOLD_MULTIPLIERS` are live, execution-adjacent dictionaries keyed on today's exact 5 labels (`TREND`/`RANGE`/`SHOCK`/`HIGH_VOLATILITY`/`UNKNOWN`); an unrecognized key silently falls through to a neutral no-adjustment default (`.get(regime, {})`), so changing what `regime` itself can be would have silently changed real fuse() weighting for every live trade. All four new fields are additive columns only, read by nothing yet.

**`EVENT_DRIVEN` scope cut, disclosed**: `classify_regime()` now accepts an optional `event_flag: pd.Series | None` parameter and a real `REGIME_EVENT_DRIVEN` constant exists, but it is NOT wired into the live equity feature pipeline (`src/features/equity_engine.py` / `equity_vectorized.py`) in this pass — doing so needs a real per-symbol company_events batch-fetch path that doesn't exist yet in the vectorized universe pipeline, and building that hastily within this same slice risked exactly the kind of scope creep "small, concrete gaps" is supposed to avoid. The mechanism is built and tested; wiring a real feed into it is follow-up work, not silently skipped.

**Real bug found and fixed while building this (not live-impacting)**: the first implementation used a literal `None` for non-trending rows in `regime_direction`. Pandas 3.0 (confirmed installed: `pandas==3.0.5`) defaults `future.infer_string=True`, which silently converts `None` into `NaN` the moment a mostly-string object column is constructed or assigned — found via a failing test (`nan == None` is `False`), not by inspection. Fixed by documenting the real contract (`pd.isna()`, not `is None`) rather than fighting the framework default.

**Files changed**: `src/scripts/backfill_candles.py`, `src/models/regime.py`, `tests/test_v4_benchmark_instruments.py` (new, 2 tests), `tests/test_regime_v4_detail_columns.py` (new, 9 tests).

**Tests**: 11 new, all passing. Full suite: **409/409 passing** (398 prior + 11 new), zero regressions.

**Execution-impact assessment**: zero. `BENCHMARK_INSTRUMENTS` only affects what the backfill script fetches (no broker writes, ever). The new regime columns are read by no code path yet — `fuse()`, `src/risk/governor.py`, `src/backtest/engine.py`, and every other `classify_regime()` caller continue to read only the three pre-existing columns, byte-for-byte unchanged.

**Known limitations, disclosed not hidden**:
- `regime_event_driven` has no real data feeding it yet anywhere live (see EVENT_DRIVEN scope cut above).
- No caller yet consumes `regime_direction`/`regime_low_volatility`/`regime_probability` — they exist for future V4 strategy-family work (Priority 3+) to read, not wired into any decision today.
- `regime_probability`'s formula is a disclosed heuristic (percentile-margin distance), not a calibrated/fitted probability — matches the architecture doc's own framing ("the percentile values already computed are a natural, cheap source for this"), not a claim of statistical rigor.

**Next**: Priority 2 (part 2) — MFE/MAE columns on `trade_outcomes` + a `rejected_signal_outcomes` table — then Priority 3 (Strategy Research Laboratory), per "go on."

---

## Priority 2 (part 2a) — MFE/MAE tracking on `trade_outcomes` — **DONE**

2026-10-08, continuing per "go on." Third of Priority 2's four items (docs/V4_ARCHITECTURE.md Section 14 gap).

**Schema**: `mfe_usd`/`mae_usd` nullable `Float` columns added to the EXISTING `trade_outcomes` table — a genuinely different operation from every other additive schema change this session, since `metadata.create_all()` only creates missing *tables*, never adds columns to a table that already exists. New one-off migration `src/scripts/migrate_v4_mfe_mae.py` (`ADD COLUMN IF NOT EXISTS`, same idempotent idiom as `migrate_p0_02_kill_switch.py`) — **run live against the real production Postgres database** and confirmed present.

**`src/outcomes/excursion.py`** (new): `compute_mfe_mae()` — pure function, no broker call — reads real stored `candles` rows (M15 preferred, falling back to H1 then H4 for older trades past M15's 60-day retention) between a trade's `opened_at`/`closed_at`, and derives the best/worst direction-adjusted USD excursion. `backfill_mfe_mae()` fills every existing `trade_outcomes` row still missing these columns, idempotently (only ever touches NULL rows).

**Real bug found and fixed via live verification against production, not by inspection**: ran the new function against 5 real closed Alpaca trades before trusting it, and found a real MSFT SELL whose realized P&L (-$569.40) came out WORSE than the "worst" MAE the function had just computed purely from stored candles (-$560.63) — the actual fill price landed outside the OHLC bar range covering that window (a genuine candle-coverage/bar-boundary gap, not a math error). Fixed by adding `exit_price` as a required input and clamping both MFE and MAE to never read better/worse than the trade's own known, certain realized outcome — re-verified against the same 5 real trades afterward, all now satisfy `mae <= realized_pl_usd <= mfe`. Added a dedicated regression test reproducing the exact scenario.

**Live-verified end to end**: ran the real backfill against production — **23 of 26 real `trade_outcomes` rows filled**, 3 skipped honestly (missing `entry_price` or `opened_at`, the known pre-existing gap — see `project_entry_price_null_fix` memory). Confirmed via direct query afterward.

**Files changed**: `src/data/db.py` (+2 columns), `src/scripts/migrate_v4_mfe_mae.py` (new), `src/outcomes/excursion.py` (new), `tests/test_v4_excursion.py` (new, 9 tests).

**Tests**: 9 new, all passing. Full suite: **418/418 passing** (409 prior + 9 new), zero regressions. The dashboard smoke test (`tests/test_dashboard_smoke.py`), which connects to the real production DB, briefly failed between adding the Python column definitions and running the live migration — exactly the signal that this isn't a `create_all()`-safe change; resolved by running the migration before any further work.

**Execution-impact assessment**: zero. No broker call anywhere in `excursion.py`; it only reads `candles` and writes `mfe_usd`/`mae_usd` on existing `trade_outcomes` rows — never touches `orders_fills`, `trade_intents`, or any execution-adjacent table.

**Known limitations, disclosed not hidden**:
- 3 real rows remain unfilled (no `entry_price`/`opened_at`) — by design, not a bug; this module doesn't fabricate a value it can't honestly compute.
- The backfill is a manual, one-off script run (`src/outcomes/excursion.py`'s `backfill_mfe_mae()`), not yet wired into the live scheduled outcome-sync task — new trades closing going forward will need either a periodic re-run or a follow-up wiring into `src/outcomes/alpaca_tracker.py`'s own write path. Disclosed as the natural next increment, not done in this slice to keep it reviewable.
- MFE/MAE accuracy is bounded by candle granularity (bars, not ticks) — the realized-P&L clamp added above handles the one place this surfaced as a real inconsistency, but the excursion *between* entry and exit (away from the exact exit point) is still a bar-level approximation, same caveat any OHLC-derived MFE/MAE system has.

**Next**: Priority 2 (part 2b) — a `rejected_signal_outcomes` table — then Priority 3 (Strategy Research Laboratory), per "go on."

---

## Priority 2 (part 2b) — "Rejected-signal hypothetical outcome tracking" — **DONE, by correcting a wrong gap finding**

2026-10-08, continuing per "go on." The last of Priority 2's four items — and the one that changed shape once actually investigated.

**The literal plan (`docs/V4_ARCHITECTURE.md` Priority 2) called for a new `rejected_signal_outcomes` table + a new scheduled job to re-price risk-rejected signals at horizon expiry.** Investigating that before building it (same "read the real code before trusting a gap claim" discipline as everything else this session) found the gap analysis itself was wrong: `src/scripts/evaluate_challengers.py`'s `_pending_champion_signals()` already selects every `trade_intents` row with `action != "NO_TRADE"` with **no status filter at all** — it re-prices `RISK_REJECTED` signals exactly the same way it re-prices approved ones, and records the result in `signal_evaluations` (`source="champion"`). **Confirmed live against production**: 8,853 of 10,778 real `RISK_REJECTED` trade_intents already have a scored `signal_evaluations` row. Building a second table and a second scheduled job would have duplicated that real, already-running pipeline rather than filled a gap.

**What was actually missing**: a way to *segment* that already-computed data by risk-decision outcome/reason — the brief's real underlying ask ("directly useful for evaluating whether the risk governor is too conservative") needs a comparison, not just raw storage. Built `src/evaluation/rejected_signal_report.py`'s `rejected_signal_segmented_report()` — a read-only query joining `risk_decisions` ⋈ `trade_intents` ⋈ `signal_evaluations` (source="champion"), grouped by `(approved, reason)`, returning `n`/`hit_rate`/`mean_move_in_favor` per group. NO_TRADE decisions are naturally excluded (they never produce a champion evaluation row to join against — verified with a dedicated test, not just assumed).

**Live-verified against real production data** — a genuinely interesting result: approved signals hit 43.1% (n=367); most rejection reasons sit close to or below that (confidence-below-threshold 48.4% n=4,671, kill-switch-active 45.7% n=2,569, stale-data 40.6% n=1,043) — no sign the governor is leaving obviously-good trades on the table at scale. Two smaller buckets (max-concurrent-positions 50.7% n=363, no-pyramid 54.5% n=55) actually out-hit the approved population, a real, disclosable signal worth a closer look in a future pass, not acted on here (this module is read-only research, never feeds back into live risk decisions).

**Real bug found and fixed via the same live-verification pass**: the first version used `avg(CAST(hit AS FLOAT))` to average a boolean column — works fine in SQLite (booleans are integer-backed there) but Postgres outright refuses a bool→float `CAST` (`CannotCoerce`). Caught immediately when run against the real production database, not by the in-memory SQLite tests (which all passed first try and would have shipped this broken). Fixed with a portable `CASE WHEN hit THEN 1.0 ELSE 0.0 END` instead.

**`docs/V4_ARCHITECTURE.md` corrected**: both the Section 14 gap-analysis row and the Priority 2 implementation-plan bullet now point at this entry instead of repeating the original, incorrect "does not exist" claim.

**Files changed**: `src/evaluation/rejected_signal_report.py` (new), `tests/test_rejected_signal_report.py` (new, 3 tests), `docs/V4_ARCHITECTURE.md` (2 corrections).

**Tests**: 3 new, all passing. Full suite: **421/421 passing** (418 prior + 3 new), zero regressions.

**Execution-impact assessment**: zero. Pure read-only query over existing tables; no write, no broker call, no new table, no new scheduled job.

**Known limitations, disclosed not hidden**:
- The two buckets where rejected signals out-hit approved ones (max-concurrent-positions, no-pyramid) are small samples (n=363, n=55) — a real finding worth tracking over time, not grounds for changing the risk governor's own thresholds on this evidence alone.
- `mean_move_in_favor` is an unweighted average of signed price moves, not risk-adjusted or cost-adjusted (no spread/slippage) — directionally informative, not a substitute for the real backtester's realistic accounting.

**Priority 2 is now fully complete** (all 4 items — benchmarks, regime detail columns, MFE/MAE, rejected-signal segmentation). **Next**: Priority 3 (Strategy Research Laboratory) or Priority 4 (opportunity scanner), per "go on" — Priority 3 is the larger, named-strategy-family work; worth confirming scope/order before diving into a multi-week slice of new strategy code.

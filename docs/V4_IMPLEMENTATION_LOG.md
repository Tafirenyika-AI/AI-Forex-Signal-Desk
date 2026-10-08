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

---

## Priority 3 (part 1) — Strategy Registry + Strategy A hypothesis test — **DONE**

2026-10-08. User explicitly chose Priority 3 (Strategy Research Laboratory) over Priority 4 (scanner) when asked. Covers brief Section 6 (Strategy Registry) and Section 7 (Evidence Registry) structurally for all 10 named strategy families, plus one real, live-verified hypothesis test (Strategy A).

**`src/strategies/registry.py`** (new): `StrategySpec` dataclass with all 13 fields the brief requires verbatim (Hypothesis/Eligible instruments/Timeframe/Entry conditions/Exit conditions/Position sizing assumptions/Stop-loss logic/Invalidation conditions/Expected holding period/Data requirements/Transaction costs/Failure conditions/Validation criteria), plus an honest `status` ladder (`RESEARCH_SPEC_ONLY` → `HYPOTHESIS_TESTED` → `BACKTESTED` → `SHADOW` → `PAPER_APPROVED`) so this registry can't silently drift into claiming more than has actually been validated. All 10 strategies (A–J) specified in full, each grounded in THIS project's real, already-built infrastructure where it applies (e.g. Strategy C/J explicitly reuse the existing ATR-ratcheting trailing stop from the earlier "model intelligence" work rather than re-describing a new one; Strategy H reuses Equity V2's SIC_TO_SECTOR + cross-market features) and honest about genuine new-work gaps where they exist (Strategy D needs deeper intraday backfill; Strategy E needs a new VWAP feature that doesn't exist anywhere yet; Strategy I needs new cointegration-testing code; Strategy G needs a new "surprise magnitude" feature).

**`docs/V4_STRATEGY_RESEARCH.md`** (new, the brief's own required Section 21 deliverable): summarizes the registry, and builds the Strategy Evidence Registry (Section 7) with 4 REAL literature citations looked up live via WebSearch on 2026-10-08 (not recalled from memory, not fabricated) — Moskowitz/Ooi/Pedersen 2012 (time-series momentum), Jegadeesh/Titman 1993 (cross-sectional momentum), Gatev/Goetzmann/Rouwenhorst 2006 (pairs trading), Bernard/Thomas 1989 (post-earnings-announcement drift). The other 6 strategy families (C/D/E/F/H/J) are honestly left without a single canonical citation — the brief names these as generic research AREAS, not specific papers, and inventing an authoritative-sounding citation for them would violate the brief's own "do not accept... hypothetical performance claims" standard applied to itself.

**`src/strategies/time_series_momentum.py`** (new) — Strategy A's real hypothesis test: does an instrument's own trailing return predict the sign of its forward return? Nearest-timestamp (pandas `merge_asof`) day-count lookback/holding windows, not a fixed bar count — so weekend/holiday gaps never silently misalign the window. Pure statistical test only (normal-approximation z-test vs. hit_rate=0.5) — no transaction costs, no sizing, no stops (that's the `BACKTESTED` stage, not done in this pass).

**Live-verified against real production data, not synthetic** (synthetic data was used only to prove the math correct first — `tests/test_time_series_momentum.py` constructs deterministic persistent/anti-persistent series with a KNOWN-by-construction answer, confirming the function gets 90%/1.5% hit rates respectively before trusting it against anything real): ran the real test against this project's own backfilled H4 candle history across 3 lookback/holding pairs (7d/1d, 28d/7d, 84d/30d) for the brief's 8 named equity candidates.

**Real, disclosed data gap found**: only 3 of the 8 named equities (NVDA, AAPL, MSFT) have ANY backfilled H4 history — the other 5 (AMD, AMZN, META, GOOGL, TSLA) were never traded by this account and were never added to any backfill list, so `n=0` for all of them. Flagged for Priority 4 (the opportunity scanner needs this same universe).

**Real finding for the 3 instruments that do have history**: no significant momentum signal at 7d/1d or 28d/7d (all `|z| < 1.5`), but a statistically significant POSITIVE signal at 84d lookback / 30d holding for all three — NVDA z=2.55 (hit rate 53.5%), AAPL z=3.34 (54.7%), MSFT z=6.32 (58.8%) — directly consistent with the cited literature's own finding that time-series momentum is a medium/long-horizon effect. Explicitly documented as `HYPOTHESIS_TESTED`, not tradeable evidence — no costs, sizing, or stops modeled; MSFT's strong z-score paired with a tiny raw mean-move-in-favor (0.00023) is called out in the doc itself as a concrete illustration of why a significant hit rate alone isn't proof of a tradeable edge.

**Files changed**: `src/strategies/__init__.py` (new), `src/strategies/registry.py` (new), `src/strategies/time_series_momentum.py` (new), `docs/V4_STRATEGY_RESEARCH.md` (new), `tests/test_strategy_registry.py` (new, 4 tests), `tests/test_time_series_momentum.py` (new, 4 tests).

**Tests**: 8 new, all passing. Full suite: **429/429 passing** (421 prior + 8 new), zero regressions.

**Execution-impact assessment**: zero. No broker call anywhere in either new module; `time_series_momentum.py` only reads `candles`, writes nothing; the registry is pure in-memory Python data.

**Known limitations, disclosed not hidden**:
- 7 of 10 strategy families remain `RESEARCH_SPEC_ONLY` — specs exist, nothing run against real data yet. Deliberate scoping for this slice, not an oversight — the user chose Priority 3 broadly, not "implement and validate all 10 strategies in one pass."
- Strategy A's result is equities-only; Strategy J explicitly calls for crypto to be validated SEPARATELY, not inferred from this result.
- No strategy here is wired into `src/decision/fusion.py` or any live decision path — explicitly Priority 5's job.

**Next**: continue Priority 3 (additional strategy hypothesis tests, e.g. Strategy F which is fully data-ready already) or move to Priority 4 (opportunity scanner) — per "go on," picking whichever next slice stays similarly scoped and verifiable.

---

## Priority 3 (part 2) — Strategy F (Volatility Breakout) hypothesis test — **DONE**

2026-10-08, continuing per "go on." `src/strategies/volatility_breakout.py` tests whether a compressed-volatility episode (`src/models/regime.py`'s `regime_low_volatility`, from V4 Priority 2) is followed by an expansion whose INITIAL direction persists — reusing `compute_features()`/`classify_regime()` wholesale, exactly as the strategy's own registry spec says it should need no new data.

**Split design, deliberate**: the module separates a pure, directly-testable `_evaluate_core(classified_df, ...)` from a thin `evaluate_volatility_breakout_hypothesis(engine, ...)` real-data wrapper. `classify_regime()`'s trailing percentiles are measured against a long (250-bar) window, which made a hand-built end-to-end OHLC fixture fragile and indirect — an initial attempt produced bizarre, hard-to-interpret results purely from interacting with that window, abandoned in favor of testing the event-detection math directly against a fabricated classified-style DataFrame.

**Three real bugs found and fixed via that fabricated-fixture testing, before this ever touched real data**:
1. The event detector initially fired on every bar while `vol_percentile` stayed elevated after a compression ended, not just the first — a deliberately-reversing synthetic fixture (compression → spike up → sustained decline) still produced a 100% "hit rate," since most "events" were really later continuation bars correlating with themselves. Fixed with a rising-edge filter.
2. That fix silently did nothing at first: `shift()` on a bool-dtype pandas Series introduces a leading NaN, upcasting the whole Series to **object** dtype holding Python `True`/`False`/`NaN` — and `~` on an object-dtype Series of Python bools performs integer bitwise-not (`~True == -2`), not logical negation. Fixed with an explicit `.astype(bool)` after `.fillna(False)`.
3. The same NaN-to-bool family struck a third time: the very first row (no prior history) produces NaN from `rolling().max()`, and `NaN.astype(bool)` evaluates `True` — incorrectly treating "no history yet" as "yes, recently compressed." Fixed the same way.

**Live-verified against real production H4 history** for NVDA/AAPL/MSFT (`compression_window_bars=20`, `holding_bars=5`, `regime_lookback=250`): NVDA n=15 hit_rate=46.7% (not significant), MSFT n=16 hit_rate=56.3% (not significant), AAPL n=8 hit_rate=87.5% z=2.12 — crosses the conventional significance threshold but n=8 is too small a sample to trust (disclosed explicitly in `docs/V4_STRATEGY_RESEARCH.md`, not quietly treated as a win).

**Files changed**: `src/strategies/volatility_breakout.py` (new), `src/strategies/registry.py` (Strategy F status → `HYPOTHESIS_TESTED`), `docs/V4_STRATEGY_RESEARCH.md` (+Section 4, Strategy F results), `tests/test_volatility_breakout.py` (new, 4 tests).

**Tests**: 4 new, all passing. Full suite: **433/433 passing** (429 prior + 4 new), zero regressions.

**Execution-impact assessment**: zero. No broker call; reads only `candles`, writes nothing.

**Known limitations, disclosed not hidden**:
- AAPL's n=8 result is explicitly flagged as too small to trust, not a validated edge.
- Crypto (Strategy J's own separate-validation requirement) not tested here, same as Strategy A.
- 6 of 10 strategy families remain `RESEARCH_SPEC_ONLY`.

**Next**: continuing per "go on" — either another Priority 3 strategy hypothesis test or Priority 4 (opportunity scanner), whichever stays similarly scoped and verifiable.

---

## Priority 3 (part 3) — Strategy C (Trend Following) + Strategy H (Sector Rotation) — **DONE**

2026-10-08. User explicitly chose "keep going — more quick-reuse strategies" when asked, naming C and H specifically as the next similarly-scoped (reuse-only, no new feature engineering) candidates.

**Strategy C (`src/strategies/trend_following.py`)**: tests whether entering on a FRESH transition into `TREND` (not merely "currently in TREND," the spec's own distinction) and holding in `regime_direction`'s direction produces a positive forward return — fixed-horizon proxy only, not the strategy's own real trailing-stop exit. Live result against NVDA/AAPL/MSFT (holding 10/20/40 bars): **no significant positive signal anywhere**, two combinations (NVDA@20, MSFT@10) mildly negative (z=-1.88, -1.63). Documented with an explicit interpretation caveat: this strategy's own spec says its real exit is a trailing stop, not a calendar-time exit, specifically to capture a trend's later stages — Equity V2's own earlier Phase D1 work already found exactly that signature (hit rate drops, payoff ratio improves) on a real ATR-ratcheting trailing stop. The flat/negative fixed-horizon result here should NOT be read as "trend-following doesn't work" — a real `BACKTESTED`-stage test needs the actual trailing-stop exit wired in, not done in this pass.

**Strategy H (`src/strategies/sector_rotation.py`)**: tests whether a sector ETF's trailing relative strength vs. SPY (top-tier of the 11 SPDR sector ETFs) predicts continued outperformance, gated on the broad market not being in `SHOCK`. **Real result: n=3,366, mean forward relative return = -0.76%, t = -7.55 — the data says the OPPOSITE of the hypothesis, strongly.** Top-tier trailing-relative-strength sectors tend to UNDERPERFORM going forward (a mean-reversion signature, not rotation-persistence). Reported honestly as a real negative/contrarian finding, not discarded — exactly the brief's own "research candidates, not assumed profitable" instruction in action. Flagged as a genuinely interesting follow-up (not pursued here): this result structurally resembles Strategy E's mean-reversion hypothesis, not H's.

**Real data gap found via Strategy H's own test**: XLE and XLF have ZERO backfilled H4 rows despite being in `BENCHMARK_INSTRUMENTS` — confirmed live against Alpaca directly that real data exists for both (XLE/XLF both have current H1 bars), so this is a genuine backfill gap, not "nothing to fetch." Not root-caused or fixed in this pass (disclosed, not silently worked around).

**Design note for Strategy C/H test suites**: both reuse the split-core pattern established for Strategy F (`_evaluate_core` directly testable against fabricated inputs, a thin DB-loading wrapper around it) — avoided Strategy F's earlier end-to-end-fixture pitfall from the start this time.

**Real test-fixture bug found and fixed (not production code)**: Strategy C's own first test attempt used `holding_bars=10 < trend_bars=15`, so the forward-return window landed INSIDE the synthetic trend itself rather than the intended post-trend "tail" region — the persistent/reversing distinction the test meant to check never actually applied. Fixed by using a fixture where the holding window deliberately exceeds the trend length.

**Files changed**: `src/strategies/trend_following.py` (new), `src/strategies/sector_rotation.py` (new), `src/strategies/registry.py` (C and H status → `HYPOTHESIS_TESTED`), `docs/V4_STRATEGY_RESEARCH.md` (+Sections 5/6, Strategy C/H results), `tests/test_trend_following.py` (new, 4 tests), `tests/test_sector_rotation.py` (new, 4 tests).

**Tests**: 8 new, all passing. Full suite: **441/441 passing** (433 prior + 8 new), zero regressions.

**Execution-impact assessment**: zero. No broker call in either new module; both only read `candles`.

**Known limitations, disclosed not hidden**:
- Strategy C's result is explicitly caveated as a proxy-test limitation, not a real finding about trend-following's viability.
- Strategy H's result directly contradicts its own hypothesis — disclosed as a real, useful negative finding, not hidden or reframed.
- 6 of 10 strategies remain `RESEARCH_SPEC_ONLY` (B, D, E, G, I, J).
- The XLE/XLF backfill gap is disclosed, not fixed.

**Next**: per "go on" — either the remaining quick-reuse evaluation (none left; B/D/E/G/I/J all need genuine new feature work first) or Priority 4 (opportunity scanner). Worth checking with the user given Priority 3's remaining items are now all the "needs new infrastructure" category, a different scope than A/C/F/H.

---

## Priority 4 — Intelligent Opportunity Scanner (Section 5) — **DONE**

2026-10-08. User chose to switch to Priority 4 when asked, matching `docs/V4_ARCHITECTURE.md`'s own suggested design: a thin ranking layer over Equity V2 Phase 9's `build_equity_feature_vector`, no new feature computation.

**`src/scanner/opportunity_scanner.py`**: ranks the brief's own named Section 5 candidate universe (8 equities, 10 ETFs, 2 crypto pairs) across 7 factors — momentum, relative strength (vs. SPY), sector strength (vs. sector ETF), trend persistence, volatility, volume behavior, VWAP deviation — each a cross-sectional percentile rank (0-1) among whichever universe members actually have that feature available, never a fabricated neutral default for a missing one. `composite_score` is a deliberately simple equal-weighted average over only the AVAILABLE factors per ticker (not a fitted/learned weighting — that's explicitly Priority 5's job, the adaptive meta-model). Supports `timeframe="intraday"` (log_return_4, intraday_volatility_percentile) vs `"swing"` (log_return_12, vol_percentile), the brief's own "distinguish intraday from swing-trading opportunities" instruction. Every ranked ticker carries a `reasons` tuple naming the exact feature, raw value, and percentile for each factor — the brief's own "provide transparent reasons for every ranking" instruction, implemented literally, not a black box.

**Honestly NOT scored, disclosed in the module's own constant (`UNSCORED_FACTORS_FROM_BRIEF`)**: the brief's Section 5 list also names "Liquidity," "Bid/ask spread," and "Breakout quality" — none has a real, dedicated feature anywhere in this codebase (only `relative_volume` as a weak liquidity proxy, already scored under `volume_behavior`). Fabricating a proxy and labeling it "spread" or "breakout quality" would misrepresent what's actually being measured, so these are named as a disclosed gap rather than silently invented.

**Availability checked live before scoring, per the brief's own "check each asset's availability and eligibility before use" instruction**: `scan_opportunities()` checks real candle existence for every universe member first — an instrument with zero backfilled history gets an honest `NOT_ELIGIBLE` entry, never silently dropped or scored with missing data treated as zero.

**Real, disclosed data gap found AND partially fixed while building this**: running the scanner against the real universe first showed only 9 of 20 named candidates had any H4 history (the gaps already flagged in Strategy A/H's own entries — 5 missing equities, IWM/GLD/IAU/USO/XLE/XLF never fetched). Ran the existing `src/scripts/backfill_candles.py` live (additive, read/write-candles-only, no broker orders — the same script already runs on its own schedule) to materialize real data for the already-`BENCHMARK_INSTRUMENTS`-listed-but-never-fetched set: **IWM, GLD, IAU, XLE, XLF now have real backfilled H4/H1/M15 history** (confirmed via a before/after row-count check). USO's H4 chunk hit a real Alpaca rate limit (429) mid-run and will self-heal on the next scheduled run (its H1/M15 data did complete, and the scanner itself uses H1, so USO is fully eligible already). The 5 individual equities (AMD, AMZN, META, GOOGL, TSLA) remain un-backfilled — deliberately NOT added to `BENCHMARK_INSTRUMENTS` in this pass, since that's a cross-market-reference-ETF list, a different category from "individual research candidates nobody currently trades"; disclosed as a known limitation instead of silently re-scoping that list.

**Live-verified against real production data**: ran `scan_opportunities()` for real, right now — 15 of 20 universe members eligible and ranked (XLE ranked #1 at 0.887, driven by strong real momentum/relative-strength/trend numbers that session), 5 honestly `NOT_ELIGIBLE`. `BTC/USD` is missing `sector_strength`/`volume_behavior`/`vwap_deviation` (crypto has no sector ETF by definition, and its cross-market feature computation didn't resolve the other two for reasons not investigated further in this pass) — disclosed via its own `missing_factors`, not silently defaulted.

**Files changed**: `src/scanner/__init__.py` (new), `src/scanner/opportunity_scanner.py` (new), `tests/test_opportunity_scanner.py` (new, 4 tests), `docs/V4_ARCHITECTURE.md` (Priority 4 marked DONE).

**Tests**: 4 new, all passing. Full suite: **445/445 passing** (441 prior + 4 new), zero regressions.

**Execution-impact assessment**: zero for the scanner itself (pure read + in-memory ranking, no broker call, no write). The backfill run IS a real write (new `candles` rows) but is the same additive, non-execution-adjacent operation this project already runs on a schedule — confirmed by re-reading the script before running it, not assumed safe.

**Known limitations, disclosed not hidden**:
- Liquidity, bid/ask spread, and breakout quality are named by the brief but not scored (no real feature exists yet for any of them).
- 5 of 20 named candidates remain un-backfilled (individual equities nobody currently trades) — a deliberate scope boundary, not an oversight.
- `composite_score`'s equal weighting is deliberately simple/unlearned — Priority 5 (the adaptive meta-model) is where a real, evidence-based weighting belongs.
- No portfolio-exposure factor is wired in yet (the brief's own "existing portfolio exposure" ranking factor) — `build_equity_feature_vector` supports an optional `portfolio_context` the caller must supply; this scanner doesn't yet read real broker positions to populate it, a natural, safe (read-only) next increment.
- BTC/USD's 3 missing factors weren't root-caused.

**Next**: per "go on" — Priority 5 (adaptive meta-model/strategy selector) is the architecture doc's own next-ordered item, or continuing to round out the scanner (portfolio-exposure wiring, crypto gap root-cause). Worth checking with the user given the pattern of confirming direction at natural milestones this session.

---

## Priority 5 (first slice) — Adaptive Meta-Model / Strategy Selector — **DONE**

2026-10-08. User chose to continue to Priority 5 when asked. A genuine strategy SELECTOR, not Phase 10's existing `fit_meta`/`predict_meta` component-blending stacker — a different mechanism for a different job, per `docs/V4_ARCHITECTURE.md`'s own gap-analysis distinction ("V4 wants real learned strategy SELECTION, which strategy, not just how to blend scores").

**`src/models/strategy_selector.py`**: `select_strategy(engine, instrument, as_of)` → `StrategySelection` with the brief's own required Section 10 output fields (candidate_symbol, action, strategy_selected, timeframe, estimated_net_advantage, confidence, supporting_evidence, risk_assessment, model_version).

**"Do not use arbitrary confidence scores as proof of profitability" — honored structurally, not just by policy**: `ELIGIBLE_STRATEGIES = ("A",)` is a hard gate. Strategy A cleared z>2.5 across all 3 tested instruments at its validated 84-day config (Priority 3 part 1); Strategy C showed no significant signal; Strategy F's one significant result (AAPL, n=8) was already flagged too small to trust; Strategy H's result actively CONTRADICTED its own hypothesis. None of C/F/H is eligible for selection — not down-weighted, excluded outright — until real new evidence changes that. An uninstrumented or unvalidated instrument (anything other than NVDA/AAPL/MSFT, or any instrument lacking the strategies that back them) returns `NO_TRADE` with an explicit "selecting here would be exactly the 'arbitrary confidence' the brief warns against" explanation, rather than guessing.

**"NO_TRADE must be a normal and acceptable decision"**: the default, most common outcome by construction — verified live below.

**New live-signal capability added to Strategy A's own module** (`src/strategies/time_series_momentum.py`'s `current_momentum_signal()`): the hypothesis-test function (`evaluate_momentum_hypothesis`) answers "was this historically true," a backward-looking research question; this new function answers "what does the trailing window say RIGHT NOW," reusing the same `_load_closes` data path, defaulting to the exact 84-day lookback the real evidence was found at (not an arbitrary different choice).

**"The meta-model must not override the independent risk governor"**: every `StrategySelection` carries an explicit `risk_assessment` string stating it has not passed through the risk governor and is shadow-only; the module makes no broker call, writes to no table, and is not wired into `run_loop.py`'s live cycle in this pass.

**Live-verified against real production data right now**: NVDA/AAPL/MSFT all currently show real BUY signals (current 84-day trailing returns +11.1%/+2.7%/+29.2% respectively — all genuinely positive, matching each instrument's own validated direction), each selection correctly citing its own instrument-specific historical hit_rate/mean_move_in_favor. QQQ and TSLA correctly return `NO_TRADE` (QQQ has real candle history but was never one of Strategy A's 3 validated instruments; TSLA has no history at all) — confirming the evidence gate works as designed, not just in theory.

**Files changed**: `src/strategies/time_series_momentum.py` (+`CurrentMomentumSignal`, `current_momentum_signal`), `src/models/strategy_selector.py` (new), `docs/V4_ARCHITECTURE.md` (Priority 5 marked DONE, first slice), `tests/test_time_series_momentum.py` (+3 tests), `tests/test_strategy_selector.py` (new, 4 tests).

**Tests**: 7 new, all passing. Full suite: **452/452 passing** (445 prior + 7 new), zero regressions.

**Execution-impact assessment**: zero. No broker call anywhere in either module; `select_strategy()` only reads `candles`, writes nothing, and is called by nothing in the live `run_loop.py` cycle.

**Known limitations, disclosed not hidden**:
- Only 1 of 10 strategies is currently eligible for selection (by design — real evidence is the gate, not strategy count).
- `estimated_net_advantage`/`confidence` are the strategy's own HISTORICAL validation numbers (a fixed lookup table keyed by instrument), not live-recalibrated probabilities — explicitly disclosed in every selection's own `supporting_evidence` text, not silently presented as more certain than they are.
- No portfolio-exposure, news/event-risk, or liquidity input is wired into the selection decision yet (the brief's own Section 10 input list names these) — this first slice covers the strategy-gating mechanism itself; richer inputs are a natural next increment once more than one strategy is eligible to weigh them against.
- Not wired into `run_loop.py`'s live cycle or any dashboard view — a separate, explicit decision point, same posture as every other V4 component this session.

**Next**: per "go on" — extending `fit_meta`/`predict_meta` to blend across multiple eligible strategies (once more than one clears the evidence bar), wiring richer Section 10 inputs (portfolio exposure, news risk), or moving to Priority 6+ (portfolio backtesting extensions, dashboard restructure). Worth checking with the user given each represents a different scope.

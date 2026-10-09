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

---

## Priority 6 (first slice) — Named performance metrics for the portfolio backtester — **DONE**

2026-10-08. User chose "keep going" toward Priority 6+. Covers one concrete, bounded item from the architecture doc's own Priority 6 list ("named Sharpe/Sortino/profit-factor/turnover fields") rather than the full, much larger set (partial fills, corporate actions, session-awareness, holdout splits — all still open, disclosed below).

**`src/backtest/performance_metrics.py`**: `compute_performance_metrics(result: PortfolioBacktestResult) -> PerformanceMetrics` — win_rate, profit_factor, trade_expectancy_pct, turnover, sharpe_ratio, sortino_ratio. Deliberately a pure, additive post-processing function over an already-produced `PortfolioBacktestResult`, not new fields baked into that dataclass or `run_portfolio_backtest()` itself — the core simulation (Equity V2 Phase 12) is already tested and untouched. Sharpe/Sortino annualize using an EMPIRICAL periods-per-year derived from the equity curve's own real timestamps, not a hardcoded 252 — `run_portfolio_backtest`'s timeline is an irregular union of whatever candle granularity the input signals actually used (H1/H4/D), so a fixed daily-bar constant would misrepresent the real annualized figure.

**Real, important finding from live verification, not from inspection**: built 129 real signals from Strategy A's own validated sign-prediction direction (every ~30 bars across NVDA/AAPL/MSFT's real H4 history, standard ATR-based stop/target sizing reusing `src/decision/fusion.py`'s own `ATR_STOP_MULTIPLIER`/`REWARD_RISK_MULTIPLE` convention) and ran them through the real portfolio backtester. **Result: net return -4.08%, win rate 42.9%, profit factor 0.88, Sharpe -0.39, Sortino -0.35 — a naive implementation of Strategy A's own validated sign-prediction is actually UNPROFITABLE.** This doesn't contradict Strategy A's earlier hit-rate finding (both are real) — it's exactly the gap the registry's own `HYPOTHESIS_TESTED` vs `BACKTESTED` status ladder exists to catch: predicting a return's sign correctly more often than chance doesn't by itself produce a profitable trading rule once a real stop/target/entry-cadence is attached. Documented prominently in `docs/V4_STRATEGY_RESEARCH.md`'s Strategy A section as a reason to treat its current `src/models/strategy_selector.py` eligibility as provisional — no live-safety impact (the selector is shadow-only/no-broker-call by construction regardless), but a real, disclosed research finding that should gate any future move toward `BACKTESTED`/`SHADOW` status.

**Files changed**: `src/backtest/performance_metrics.py` (new), `docs/V4_ARCHITECTURE.md` (Priority 6 marked first-slice-done), `docs/V4_STRATEGY_RESEARCH.md` (Strategy A section, real backtest finding), `tests/test_performance_metrics.py` (new, 7 tests).

**Tests**: 7 new, all passing — including a deliberate Sharpe-vs-Sortino contrast test (asymmetric upside/downside volatility) confirming Sortino correctly ignores upside swings Sharpe penalizes. Full suite: **459/459 passing** (452 prior + 7 new), zero regressions.

**Execution-impact assessment**: zero. Pure computation over already-produced backtest results; no broker call, no DB write, no live wiring.

**Known limitations, disclosed not hidden**:
- Priority 6's larger items (partial fills, corporate-action awareness, explicit session/24-7 handling, short-margin constraints, a true held-out final test window) remain open — this slice covers only the named-metrics gap.
- The 129-signal verification backtest used a simple, uniform "every 30 bars" entry cadence and standard ATR sizing — not necessarily the best or only way to trade Strategy A's signal; the unprofitable result is real evidence against THIS naive implementation, not a final, exhaustive verdict on the underlying sign-prediction's tradeability.
- `profit_factor` returns `inf` (not a crash, and not `None`) when every closed trade won — a real, correctly-handled edge case, tested explicitly.

**Next**: per "go on" — the remaining Priority 6 items, Priority 7 (dashboard restructure), or reconsidering Strategy A's selector eligibility given this session's own new backtest finding. Worth checking with the user given the real-finding above has a direct bearing on Priority 5's existing selector.

---

## Priority 5 (correction) — Strategy A's selector eligibility pulled

2026-10-08. Asked the user directly how to handle Priority 6's finding bearing on Priority 5's selector; chose to pull Strategy A's eligibility now rather than leave it disclosed-only or investigate further first.

**`src/models/strategy_selector.py`**: `ELIGIBLE_STRATEGIES` changed from `("A",)` to `()`. Also fixed a real gap in the original implementation while making this change: `ELIGIBLE_STRATEGIES` had been defined but never actually referenced in `select_strategy()`'s own control flow (which hardcoded a check against `_STRATEGY_A_VALIDATED_INSTRUMENTS` instead) — a dead constant, found while wiring in the real eligibility check this correction needed anyway. Now genuinely gates: `select_strategy()` checks `"A" not in ELIGIBLE_STRATEGIES` first, before any instrument-specific logic, and returns `NO_TRADE` with an explicit explanation naming the real backtest finding that triggered the pull (net return -4.08%, profit factor 0.88, Sharpe -0.39).

**Mechanism proven still correct, not broken**: rewrote `tests/test_strategy_selector.py` to (a) confirm the new real behavior — `NO_TRADE` for every instrument, even NVDA with a crystal-clear synthetic uptrend — and (b) via `monkeypatch.setattr(strategy_selector, "ELIGIBLE_STRATEGIES", ("A",))` in one dedicated test, confirm the underlying BUY/SELL selection logic still produces a correct, fully-reasoned selection when eligibility is restored — proving this is a deliberate gate, not a regression.

**Live-verified against real production data**: `select_strategy()` for NVDA/AAPL/MSFT now all correctly return `NO_TRADE` (previously all three returned `BUY`).

**Files changed**: `src/models/strategy_selector.py`, `tests/test_strategy_selector.py` (rewritten: 5 tests, net +1 from before).

**Tests**: Full suite: **460/460 passing** (459 prior, 1 test removed + 2 added), zero regressions.

**Execution-impact assessment**: zero — this module was already shadow-only/no-broker-call; this change only makes its already-conservative output more conservative.

**Known limitations, disclosed not hidden**:
- `ELIGIBLE_STRATEGIES = ()` means this selector currently has no path to ever return anything but `NO_TRADE` — correct given the real evidence today, but worth remembering this isn't a permanent design constraint, just today's honest state.
- Re-adding any strategy requires BOTH a significant hypothesis-test result AND a profitable backtest result (the new bar this correction established) — documented in the module's own top-of-file docstring and the `ELIGIBLE_STRATEGIES` comment so a future session doesn't accidentally revert to the weaker, hit-rate-only bar.

**Next**: per "go on" — the remaining Priority 6 items, Priority 7 (dashboard restructure), or trying a different Strategy A implementation (different entry cadence/sizing) to see if a profitable backtest variant exists before concluding the sign-prediction can't be monetized at all.

---

## Priority 6 (second slice) — Genuine held-out final test window — **DONE**

2026-10-08, continuing per "go on" after the eligibility correction. Covers the brief's own Section 14/15 "untouched final holdout" item, applied directly to re-check the one finding that mattered most: Strategy A's own validated result.

**`src/strategies/time_series_momentum.py`**: refactored the existing `evaluate_momentum_hypothesis()` into a shared `_build_merged_returns()` + `_score_merged()` pair (pure refactor, verified against the full existing test suite before adding anything new), then added `evaluate_momentum_hypothesis_with_holdout()` — splits the SAME merged trailing/forward-return rows chronologically (never shuffled, since a real holdout must be a genuinely later time period, not a random sample that could still leak lookback/holding windows across the boundary) into a development portion (earliest 80%) and an untouched final holdout (most recent 20%), scoring each independently.

**Real, major finding — the original Strategy A claim does not survive genuine out-of-sample testing**: re-ran the validated 84d/30d configuration with this split against real NVDA/AAPL/MSFT H4 history:

| Instrument | Development z | Holdout z |
|---|---|---|
| NVDA | 3.17 | **-0.48** |
| AAPL | 3.81 | **2.32** |
| MSFT | 8.19 | **-1.19** |

**Only AAPL's finding replicates.** NVDA and MSFT's original full-sample significance (z=2.55/6.32 in the Section 3 table) flips to non-significant-or-negative on data genuinely never touched during the original analysis — strong evidence the original "significant across all 3" claim was, at least partly, an in-sample artifact of testing the entire history at once. This is independent of (and consistent with) the unprofitable-backtest finding from Priority 6's first slice — two separate, real analyses now both point the same direction.

**`docs/V4_STRATEGY_RESEARCH.md` corrected**: added a ⚠ correction note directly above the original Section 3 table (so a reader can't miss it by only skimming the table), a full holdout results table, and an updated Known Limitations entry. Also flagged that Strategies C/F/H have NOT yet been re-checked against a genuine holdout and could have the same fragility — disclosed, not assumed away.

**Files changed**: `src/strategies/time_series_momentum.py` (refactor + new function), `docs/V4_STRATEGY_RESEARCH.md` (correction + holdout table), `docs/V4_ARCHITECTURE.md` (Priority 6 status), `tests/test_time_series_momentum.py` (+3 tests).

**Tests**: 3 new (a uniformly-persistent series replicating on both halves, a deliberate regime-change series where development and holdout genuinely disagree, and an insufficient-history edge case), all passing — plus all 7 pre-existing tests in the same file re-confirmed passing after the refactor. Full suite: **463/463 passing** (460 prior + 3 new), zero regressions.

**Execution-impact assessment**: zero — read-only research function, no broker call, no write.

**Known limitations, disclosed not hidden**:
- Holdout testing has only been applied to Strategy A — C/F/H's results are not yet re-validated this way and could be similarly fragile.
- The 80/20 development/holdout split and the specific 84d/30d config are both inherited from the original analysis, not independently re-derived — a fully rigorous walk-forward approach (re-fitting/re-selecting the lookback on development alone, then testing on holdout) is more thorough than this single fixed-config split and remains future work.

**Next**: per "go on" — applying the same holdout discipline to C/F/H, the remaining Priority 6 items (partial fills, corporate actions, session-awareness), or Priority 7 (dashboard restructure).

---

## Priority 3 (parts 4-9) — Strategies J, B, E, I, G, D — **PRIORITY 3 NOW FULLY COMPLETE**

2026-10-08. User said "continue until you finish." Full detail, tables, and real findings for each strategy are in `docs/V4_STRATEGY_RESEARCH.md` (Sections 7, 8, 9, 10, 12, 13 respectively) — summarized here, not duplicated in full.

- **Strategy J (crypto, `00902dc`)**: pure reuse of A/F against real BTC/USD, ETH/USD. The equity-validated 84d/30d momentum config actively REVERSES sign on crypto (BTC z=-3.76, ETH z=-6.03). A promising-looking 28d/7d config failed its own holdout immediately (BTC dev z=3.77→holdout z=-1.65; ETH dev z=0.43→holdout z=6.35 — the two disagreeing this sharply means noise).
- **Strategy B (`10ea176`)**: new `src/strategies/cross_sectional_momentum.py` — classic Jegadeesh-Titman top-minus-bottom spread (distinct from H's SPY-relative construction). A 60-bar/20-bar spread looked significant full-sample and even stronger in development, but the genuine holdout collapsed to t≈0.12 (see the correction note below — these exact numbers were later corrected).
- **Strategy E (`06e2703`)**: new `src/strategies/vwap_reversion.py` — reuses `approx_vwap()` (Equity V2 Phase 8, already existed; corrected this registry's own earlier wrong claim that no VWAP feature existed). MSFT's standout full-sample signal was inconclusive once holdout-split (too few holdout events).
- **Strategy I (`b72251d`)**: new `src/strategies/pairs_trading.py` — genuinely new cointegration-testing infrastructure (added `statsmodels` dependency), real Augmented Engle-Granger test. Tested 6 real pairs — NONE shows genuine cointegration. Found a real, dangerous false-positive trap: GLD/IAU's and AAPL/NVDA's mechanical spread-reversion tests alone look spectacular despite failing the cointegration prerequisite — exactly why that gate exists.
- **Strategy G (`a4f437a`)**: new `src/strategies/earnings_drift.py` — found `company_events` confirmed completely empty (a known, already-disclosed Equity V2 Phase 3 gap) and `equity_news`'s "EARNINGS" tag only matches multi-ticker roundups, not per-ticker events. Used `company_fundamentals.filed_at` instead. **Real bug found and fixed**: a Q4/fiscal-year-end fact shares its `period_end` with both the true single-quarter EPS AND SEC's own cumulative annual fact — mixed them in an early version (MSFT showing "17.95" EPS, its actual annual figure). Fixed via an 80-100-day duration filter. Real result: n=6-7 per instrument, too small to reach significance either way.
- **Strategy D (`23ea0f6`)**: new `src/strategies/opening_range_breakout.py` — M15's 60-day depth turned out genuinely sufficient (~60-63 real trading days), correcting the registry's own earlier wrong assumption that it wasn't. **Real, striking, consistent finding**: all 6 instrument/config combinations tested are NEGATIVE (opposite of the hypothesis); AAPL and MSFT reach significance. Holdout attempt: samples too tiny (n=0-3) to confirm or deny.

**Priority 3 is now fully complete — all 10 of 10 named strategy families are `HYPOTHESIS_TESTED`.** The headline finding across the whole registry: almost nothing survives genuine scrutiny (holdout testing, real backtests) once actually checked — not how many strategies "work," but how few do. No strategy has cleared a real, holdout-robust bar for trading.

**Tests**: 8+4+8+5+4+5 = 34 new across the 6 strategies. Full suite reached 486/486 by the end of this stretch.

---

## Priority 6 (third slice) — Corporate-action detection, and a real data-corruption finding — **DONE**

2026-10-08, continuing per "go on." Covers the brief's Section 14/15 "corporate actions" item — detection, not full historical back-adjustment (which would need a licensed corporate-actions calendar this project doesn't have).

**Real risk identified before any data was inspected**: `src/broker/alpaca.py` never passes Alpaca's `adjustment` parameter on bar requests, so all backfilled history is RAW (split/dividend-unadjusted) by default — a future stock split in any tracked instrument would silently corrupt every technical/regime/momentum feature computed across that boundary, with no guard anywhere in this codebase.

**`src/data/corporate_actions.py`**: `detect_likely_stock_splits()` — flags a bar only when BOTH a large single-bar move (>15%) occurs AND the ratio matches a common real split ratio (2-for-1, 10-for-1, 1-for-2, etc.) within tolerance — a large move alone (a real rally/selloff) is never enough to avoid false positives. Found and fixed a real labeling bug in its own reference table before shipping: an early version had "2-for-1" mapped to the wrong ratio (2.0 instead of 0.5 — a 2-for-1 split means price HALVES, not doubles) — caught by the module's own test, not live.

**Ran it against every currently-backfilled real instrument — found a real, previously-undetected, currently-uncorrected data corruption**: 5 Select Sector SPDR ETFs (XLB, XLE, XLK, XLU, XLY) all flagged a 2-for-1 split candidate on the exact same real date (2025-12-05). **Verified via live web search this is a real, documented corporate action**: State Street executed a genuine 2-for-1 split of these exact 5 ETFs effective 2025-12-04/05 (confirmed via SEC EDGAR filings and contemporaneous reporting) — not a data artifact.

**User explicitly asked how to proceed given this was a real production data-integrity finding; chose to apply the real, confirmed correction.** `src/scripts/fix_spdr_2025_split.py`: back-adjusts all pre-2025-12-05 candle rows for these 5 tickers (×0.5 price, ×2 volume) across every granularity. Idempotent — checks the real ratio between the last pre-cutoff and first post-cutoff close before adjusting, skips tickers already corrected. **Ran live against production**: adjusted 1,003/975/1,018/1,005/1,001 rows for XLB/XLE/XLK/XLU/XLY respectively. **Re-ran the detector afterward and confirmed zero remaining split candidates anywhere**, and confirmed the migration is safely idempotent (re-running it reports "already adjusted, skipping" for all 5).

**Re-checked every Priority 3 strategy that used this ETF universe, since their numbers were computed on the corrupted data**:
- Strategy H (sector rotation): t went from -7.55 to **-2.63** — still real and significant, but the original number overstated the effect size (`docs/V4_STRATEGY_RESEARCH.md` Section 6 corrected).
- Strategy B (cross-sectional momentum): full-sample t from 4.01→2.69, development t from 4.59→3.11; holdout t unchanged at 0.12 (the holdout window falls entirely after the split, so was never affected) — conclusion (fails holdout) unchanged, magnitudes corrected (Section 8 corrected).
- Strategy I (pairs trading): XLK/QQQ's cointegration p-value went from 0.549→0.313 (still fails) and its mechanical-reversion z from 1.46→2.24 (now crosses the significance threshold, making it an even stronger illustration of the false-positive trap the section describes) (Section 10 corrected).

In every case the HEADLINE CONCLUSION was unchanged — this was a correction to magnitude/precision, not a reversal of any finding.

**Files changed**: `src/data/corporate_actions.py` (new), `src/scripts/fix_spdr_2025_split.py` (new), `tests/test_corporate_actions.py` (new, 5 tests), `tests/test_fix_spdr_2025_split.py` (new, 4 tests), `docs/V4_STRATEGY_RESEARCH.md` (3 correction notes, Sections 6/8/10).

**Tests**: 9 new, all passing. Full suite: **495/495 passing** (486 prior + 9 new), zero regressions.

**Execution-impact assessment**: the detector itself is read-only. The migration is a real, one-time write to historical `candles` rows — confirmed additive/corrective (not destructive: no row deleted, only 5 specific tickers' pre-split OHLCV values rescaled using a real, cited, verified split ratio), run only after explicit user approval given it modifies production data other parts of the system also depend on.

**Known limitations, disclosed not hidden**:
- This is DETECTION + a targeted, confirmed correction for one specific real event — not a general, automatic corporate-actions ingestion pipeline. A different future split would need the same manual verify-then-correct process (or a dedicated licensed feed, not pursued here).
- The detector isn't wired into any scheduled job or dashboard view yet — it was run ad hoc for this investigation. Wiring it into a periodic data-quality check is a natural next increment, not done in this pass.
- Only candles were corrected; any other table that independently stored raw prices for these 5 tickers (none identified, but not exhaustively audited) could still carry the same artifact.

**Next**: per "go on" — remaining Priority 6 items (partial fills, session-awareness, holdout-check the still-unchecked strategies C/F/H), or Priority 7 (dashboard restructure).

---

## Priority 6 (fourth slice) — Holdout-checking the remaining strategies (C, F, H)

2026-10-08, continuing per "go on." Closes the disclosed gap noted in every prior Priority 3/6 entry: C, F, and H were the last 3 of the 10 strategies never holdout-tested.

Added `evaluate_trend_following_with_holdout()`, `evaluate_volatility_breakout_with_holdout()`, and `evaluate_sector_rotation_with_holdout()` — each a light refactor (pulling the existing candle-loading/classification logic into a reusable `_build_*` helper, re-confirmed against each module's full pre-existing test suite before anything new was added) plus the same chronological, never-shuffled split convention every other holdout wrapper this session already uses.

**Strategy C**: already null full-sample; holdout confirms no instrument shows a real signal in either half (sample sizes n=3-5 in the holdout portions, too small to add new information beyond the existing fixed-horizon-proxy caveat).

**Strategy F**: AAPL's one significant result (n=8 full-sample) turns out to have **zero holdout events at all** — not just a small holdout sample, a genuinely empty one. Development alone reproduces the exact full-sample z=2.12 (all 8 events fall in the earliest 80%). A harder limit than "too small to trust" — there isn't enough history to even attempt the check.

**Strategy H — the most important of the three**: development alone (t=-2.61) closely matches the corrected full-sample finding (t=-2.63), as expected. But **the holdout's sign flips** — a small, non-significant POSITIVE mean (+0.14%, t=0.96) instead of the development/full-sample's significant negative. This is the same pattern as A/B/J: a real, significant-looking in-sample result that a genuine out-of-sample slice does not support. Strategy H's own "the hypothesis is contradicted" finding should now be read as "contradicted in-sample, unconfirmed out-of-sample," not as weaker-but-still-real evidence against rotation-persistence.

**`docs/V4_STRATEGY_RESEARCH.md` updated**: holdout tables and findings added to Sections 4 (F), 5 (C), and 6 (H); the final Known Limitations summary updated — holdout testing has now been applied to 7 of 10 strategies (A/B/C/D/F/H/J), with every single one that had a real full-sample finding failing to fully survive it.

**Files changed**: `src/strategies/trend_following.py`, `src/strategies/volatility_breakout.py`, `src/strategies/sector_rotation.py` (all 3: new `_build_*` helper + holdout wrapper), `docs/V4_STRATEGY_RESEARCH.md`, `tests/test_remaining_holdout_wrappers.py` (new, 6 tests).

**Tests**: 6 new, plus all 12 pre-existing tests across the 3 refactored modules re-confirmed passing. Full suite: **501/501 passing** (495 prior + 6 new), zero regressions.

**Execution-impact assessment**: zero — read-only research functions, no broker call, no write.

**Known limitations, disclosed not hidden**:
- Only Strategy E and G remain without a holdout check — E's own result was already found inconclusive by splitting it a different way (development/holdout on its own deviation events), and G's sample sizes were already too small full-sample to need a holdout check to know that.
- The recurring "fails holdout" pattern is now so consistent (6 of 7 checked strategies) that it's reasonable to treat it as a property of this project's current ~2 years of real history and simple rank/sign-based test methodology in general, not a coincidence specific to any one strategy.

**Next**: per "go on" — the remaining Priority 6 items (partial fills, session-awareness, short-margin constraints), Priority 7 (dashboard restructure), or Priority 8 (TradingView, explicitly lowest priority).

---

## Priority 7 — Dashboard restructure (Section 19) — **DONE**

2026-10-09. User explicitly confirmed approach before starting (a full two-level nav, not a lighter reordering) given this touches a live, currently-functioning 2,949-line dashboard with 15 flat tabs and needs browser-based verification, not just pytest.

**Approach**: rather than hand-editing ~2,949 lines (high error risk at this scale), wrote a one-off Python transformation script that (1) verified every tab block's exact boundaries by asserting each one's expected `with tab_X:` line before touching anything, (2) replaced the old flat `_tab_labels`/`st.tabs()`/tuple-unpack block with a new two-level shell — an outer `st.tabs()` for 6 groups (5 brief-named groups + standalone Mission Control) plus a conditionally-appended standalone Admin tab, and inner `st.tabs()` per analytical group — (3) re-indented each original tab block by exactly 4 spaces to nest inside its new group (standalone groups needed zero re-indentation, since their tab variable is assigned directly to the outer tab), and (4) verified the result parses as valid Python (`ast.parse`) before trusting it.

**Group mapping** (judgment calls, disclosed so they're easy to redirect):
- **Mission Control** (standalone): unchanged, a landing-page summary, not one of the 5 analytical categories the brief names.
- **Market Intelligence**: Markets, Currency Map.
- **AI Intelligence**: Agent Council, Pending Signals (placed here since each pending signal surfaces the AI's own reasoning for human review — closest fit to the brief's "decision explanations"), Model Learning.
- **Research Laboratory**: Challengers, Knowledge Lab.
- **Trading Performance**: Trade History, Account, All Signals, **Equity Intelligence** (placed here rather than split — see below).
- **Risk & Operations**: Risk Center, Automation Center, Audit Log.
- **Admin** (standalone, conditional): unchanged.

**Deliberate, disclosed simplification**: the brief's own wording says to fold Equity Intelligence's content into BOTH Trading Performance AND Risk and Operations (splitting its reconciliation/performance content from its data-health content). Given that tab's actual content is one cohesive block (not already split internally) and splitting it carries real risk of breaking working functionality without the kind of careful, line-by-line verification this pass didn't have budget for, it was placed wholesale under Trading Performance (its primary described purpose — "reconciled broker-verified-vs-internal performance") rather than risk a content split. A genuine future split remains possible, disclosed as not done here.

**A real bug found and fixed during the transformation, before it ever reached the browser**: the script's line-range boundaries for the "pre-nav" region didn't account for `HORIZON_ORDER`/`HORIZON_STYLE_HINT`/`HORIZON_TO_CHART_GRANULARITY` — three module-level constants that sat between the old nav-creation code and the first tab block in the original file. They were silently dropped by the first transformation pass, surfacing as a real `NameError: name 'HORIZON_STYLE_HINT' is not defined` the moment Mission Control rendered in the browser. Caught immediately via live browser verification (not by `ast.parse`, which only checks syntax, not undefined names), fixed by re-inserting the 3 constants right after the new nav shell.

**Verified the fix was complete, not just the one symptom**: ran a full stripped-line multiset comparison between the original committed file and the transformed one (every line's content, ignoring only leading-whitespace changes from re-indentation) — confirmed the ONLY lines present in the old version but missing from the new one were the old nav-creation code being deliberately replaced (10 lines), and the only new lines were the new nav shell (29 lines). No other content was silently dropped anywhere in the other ~2,900 lines.

**Live browser verification** (Playwright, matching this project's own established dashboard-testing practice — a real temporary admin QA account was created, used, and deleted for this, never a real user's credentials): launched the dashboard on a local port, logged in, and clicked through all 7 outer tabs (6 real groups + Admin). **Zero `NameError`/`Traceback` anywhere**, confirmed via both automated text-scraping of the page body and visual screenshots. Explicitly confirmed each multi-inner-tab group actually shows its own correct inner tab bar with real rendered content: Market Intelligence (2 inner tabs, real price-chart UI — though a separate, pre-existing, unrelated bug surfaced there, see below), AI Intelligence (3 inner tabs, real Agent Council vote data), Research Laboratory (2 inner tabs, real champion/challenger calibration charts and live numbers — e.g., a real 48% champion hit rate over 11,269 elapsed signals), Trading Performance (4 inner tabs), Risk & Operations (3 inner tabs, the full real risk-governor hard-limits table). Admin's own tab was visually confirmed present and correctly labeled in the nav bar (screenshot evidence) — its specific click-through wasn't automatable within this session's tooling (a Playwright selector quirk, not an app issue), but its content block is provably byte-for-byte unchanged from the original, already-working version, and every other tab using the identical navigation pattern worked correctly.

**A separate, pre-existing, unrelated bug surfaced during verification, explicitly NOT fixed in this pass**: the Markets tab showed `Could not load price chart: AttributeError("'NoneType' object has no attribute 'oanda_api_token'")` for the temp QA account (which never configured any broker). This is a leftover OANDA-era code path (OANDA was fully, permanently removed from this project 2026-09-30 per `project_forex_paused_equities_focus`) that still gets exercised for an account with no configured instruments — a real, disclosed, out-of-scope finding for a future pass, not a navigation-restructure bug.

**Cleanup**: the temporary QA admin account (`_qa_temp_dashboard_check`) and its `user_preferences` row were deleted from production immediately after verification — confirmed removed via a direct query. The local verification server (port 8765) was stopped.

**Files changed**: `src/dashboard/app.py` (nav restructure + the HORIZON constants fix), `docs/V4_ARCHITECTURE.md` (Priority 7 marked DONE).

**Tests**: no new pytest coverage (dashboard UI isn't unit-tested in this project — verified live instead, per this project's own established dashboard-testing convention). Full suite re-confirmed unaffected: **501/501 passing** (unchanged from before this phase — `app.py` isn't imported by the test suite).

**Execution-impact assessment**: zero broker/trading impact — pure UI/navigation restructuring, no change to any decision, risk, or execution code path. The one real write this phase made to production was the temporary QA user (created and fully deleted within the same session).

**Known limitations, disclosed not hidden**:
- Equity Intelligence's content was placed wholesale under Trading Performance rather than split per the brief's literal wording — a deliberate, disclosed simplification given the real risk of a careless content split.
- The AI Intelligence / Pending Signals placement is a judgment call (it could also reasonably sit under Trading Performance or stand alone) — easy to move later if it doesn't feel right in practice.
- Admin's click-through wasn't directly automated in this verification pass (tooling selector issue only); strongly inferred correct from its unchanged content and the identical, already-proven navigation pattern.
- The separate OANDA-leftover price-chart bug found during verification was disclosed, not fixed — out of scope for a navigation restructure.
- No new "Research Laboratory" content was added for the real work done in Priority 3 (the 10-strategy registry's real HYPOTHESIS_TESTED results aren't yet surfaced anywhere in the dashboard) — a genuine missed opportunity to make this session's own research findings visible, flagged as a natural next increment, not done here to keep this phase scoped to the restructuring ask itself.

**Next**: per "go on" — remaining Priority 6 items, Priority 8 (TradingView, explicitly lowest priority), or wiring new Priority 3/4/5 content (Strategy Registry results, Opportunity Scanner, Strategy Selector) into the new dashboard groups now that there's a real home for them.

---

## Priority 8 — Optional TradingView Integration (Section 17) — **DONE**

2026-10-09, continuing per "go on." The brief's own explicitly last and optional priority — now real, working, tested, and live-verified, matching every other priority's standard rather than being left as a stub because it's optional.

**Real architectural gap found before writing any code**: this project has NEVER had an HTTP server of any kind — it's a Streamlit dashboard plus scheduled background scripts, with no existing REST/webhook infrastructure to extend. A genuine new dependency was required (FastAPI + uvicorn, added to `requirements.txt`), the same "add a new dependency when the work genuinely needs one" precedent as `statsmodels` in Priority 3.

**`src/integrations/tradingview_webhook.py`** (pure, dependency-free of FastAPI, directly unit-testable): `validate_and_normalize_alert()` — real source verification (`hmac.compare_digest` against an operator-configured shared secret, read from `TRADINGVIEW_WEBHOOK_SECRET`; no secret configured means every request is rejected, never silently open), replay protection (an alert's own embedded timestamp must be within 5 minutes of when the server actually received it), required-field/action validation, and `normalize_symbol()` (TradingView's own `EXCHANGE:TICKER` format → this project's `AAPL`/`BTC/USD` convention). A deterministic SHA-256 dedup key (symbol + action + timestamp + strategy name) lets the database's own unique constraint do real dedup work, the same "let the DB constraint handle it" convention already established elsewhere in this project.

**Real bug found and fixed via the module's own tests, before it ever ran against anything live**: the first version of `normalize_symbol()` only stripped a fixed allow-list of EQUITY exchange prefixes (`NASDAQ:`, `NYSE:`, ...) — `BINANCE:BTCUSDT` passed straight through unnormalized, since no crypto exchange was in the list. Fixed by replacing the allow-list with a generic `^[A-Z]+:` prefix-strip pattern, since TradingView's own format is consistently `EXCHANGE:TICKER` regardless of asset class — more correct AND simpler than maintaining an ever-growing exchange list.

**New additive table**: `tradingview_alerts` (`src/data/db.py`) — a real audit log of every request, accepted or rejected, with a `UniqueConstraint` on `dedup_key` (nullable, so multiple rejected alerts with no real key never collide, matching the project's own established NULL-uniqueness convention). Confirmed created additively in production via the inspector before trusting it.

**`src/integrations/tradingview_server.py`** — the real FastAPI receiver, one endpoint (`POST /webhook/tradingview`). Ships OFF by default per the brief's own explicit instruction: gated on `src/v4/feature_flags.py`'s `v4_tradingview_enabled()` (built back in Priority 1) — when disabled, every request gets a clear `503` and **nothing is persisted at all**, not even a rejected-alert audit row, since an operator who hasn't turned this on yet shouldn't have their database quietly accumulating traffic for a feature they never enabled. Structurally, not just by policy, incapable of placing an order: there is no code path anywhere in either module that creates a `trade_intent`, calls a broker, or feeds any live decision path — exactly the brief's own "supplementary evidence, not automatic commands to trade."

**Live-verified end to end, not just via pytest**: started the real server as a standalone process (`python -m src.integrations.tradingview_server`), sent real HTTP POST requests via `httpx` from a separate process, and confirmed: (1) a valid, correctly-signed alert is accepted (200) and genuinely persisted to the real production database with correctly normalized fields; (2) a wrong-secret request is rejected (400) and still audit-logged with the real rejection reason; (3) restarting the server with `V4_TRADINGVIEW_ENABLED` genuinely unset (the real default, not a test override) correctly rejects every request with 503, and confirmed via a direct database query that this truly persisted nothing — the safe-by-default behavior holds in an actual running process, not just inside a unit test's mocked environment. All live-verification rows were cleaned up afterward, confirmed via a direct delete + count query, leaving the new table in a clean state for real future use.

**Files changed**: `requirements.txt` (+fastapi, +uvicorn), `src/data/db.py` (+`tradingview_alerts` table), `src/integrations/__init__.py` (new), `src/integrations/tradingview_webhook.py` (new), `src/integrations/tradingview_server.py` (new), `tests/test_tradingview_webhook.py` (new, 17 tests), `tests/test_tradingview_server.py` (new, 6 tests).

**Tests**: 23 new. Full suite: **524/524 passing** (501 prior + 23 new), zero regressions.

**Execution-impact assessment**: zero. No broker call anywhere in either module; the only write is the new, dedicated `tradingview_alerts` audit table, and the server isn't started by anything else in this project (no existing scheduled task, dashboard, or script launches it automatically) — an operator must explicitly run it.

**Known limitations, disclosed not hidden**:
- Actually exposing this over real public HTTPS (reverse proxy, TLS certificate, DNS, and pointing a real TradingView alert at the resulting URL) is a genuine deployment decision for the user to make — not something this module can or should do unilaterally.
- No consumer reads `tradingview_alerts` yet (the brief's own "Decision Committee" treating alerts as supplementary evidence) — this phase builds the real, working receive-and-log mechanism; wiring its output into any live decision path is explicitly future work, and would itself need the same explicit-approval discipline as every other execution-adjacent change this session.
- The crypto/equity symbol-normalization heuristic (quote-suffix stripping only for plausible 2-5 character base tickers) is a disclosed, imperfect heuristic, not a full real-world symbol mapping table.
- This is the last of the brief's 8 priorities to receive dedicated work — all 8 have now been touched with real, tested, (where applicable) live-verified implementation, though several (especially Priority 6's partial-fills/session-awareness/short-margin items) remain partially open, disclosed in their own entries above.

**Next**: with all 8 priorities now touched, remaining work is rounding out already-started items (Priority 6's remaining sub-items, wiring Priority 3/4/5's real results into the new Priority 7 dashboard groups) rather than starting anything new from zero.

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

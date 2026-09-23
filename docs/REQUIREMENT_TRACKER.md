# Requirement tracker — external review brief, 2026-09-22

Source: "AI Forex Signal Desk — Claude implementation brief" (reviewed revision `890241a67e37d8fc80f42fee7f9aea1276a5e3a2`, which was confirmed identical to this repo's HEAD when this work started — no drift to reconcile).

Branch: `hardening/p0-order-safety` (isolated per the brief's own instruction; not merged to `main`, no deployment, no live-account risk-setting changes, no real-money orders — all per the brief's explicit authorization limits).

Status legend: **DONE** (implemented + tested, evidence below) · **PARTIAL** (real progress, real gap remains — see "Remaining") · **NOT STARTED**.

This is Phase A (current-state verification) plus a first slice of Phase B (P0-01). The brief itself frames this as a multi-week, multi-phase effort (Phases A–F) — this session covers one bounded, fully-verified unit of it, not the whole brief. Everything below "P0-01" is NOT STARTED, tracked here so the next session picks up with full context rather than re-deriving it.

---

## P0-01 — correct exposure and reserve risk atomically — **PARTIAL**

### Sub-finding 1: notional-vs-P&L-sensitivity unit bug — **DONE**

**Real bug confirmed exactly as the brief described**: `src/run_loop.py`'s `compute_exposure()` was calling `risk_governor.usd_value_per_unit()` — a function whose job is "USD P&L per 1-unit price move" (correct for stop-distance risk sizing) — as if it answered "USD value of 1 unit of position" (notional). These coincide only for quote-is-USD FX pairs; every other case was wrong:
- USD_JPY (base=USD) at 150: reported ~$666.67 for 100,000 units instead of the real $100,000 (T01, exact match to the brief's reproduction).
- Equity/crypto (both always return 1.0 from the old function): a $200-share, 100-share position reported "$100 exposure" instead of the real $20,000 (T02, exact match).

**Consequential downstream effect found while fixing this** (not itself described by the brief's own text, but implied by it): `MAX_EQUITY_CRYPTO_NOTIONAL_PCT`'s "no-leverage ceiling" (added 2026-09-18, a previous session) reads `already_open` from this same buggy `compute_exposure()` output for equities/crypto — i.e., it was comparing a raw **share count** against a **dollar** headroom. Once a second same-direction position existed, its contribution was undercounted by orders of magnitude, silently defeating the ceiling for anything beyond a single isolated trade. Fixed by the same correction (no separate change needed — same root cause).

**Implementation**:
- `src/risk/governor.py`: new `usd_notional_per_unit(pair, price, usd_rates)`, distinct from the unchanged `usd_value_per_unit` (still correctly used everywhere else it's called — sizing, unrealized P&L, R-multiple calc).
- `src/run_loop.py`: `compute_exposure()` now calls the new function.

**Tests**: `tests/test_risk_exposure.py` (11 tests) — T01, T02 exactly, plus crypto (same bug class), quote-is-USD (unchanged-correct case), true cross pairs (uses the BASE currency's rate, not quote — a real distinction from `usd_value_per_unit`'s own cross-pair handling), and a multi-position aggregate sum.

### Sub-finding 2: correlation gate basis — **DONE**, per an explicit user decision

Fixing the notional bug above would have silently and drastically changed `MAX_CORRELATED_EXPOSURE_PCT`'s (5%) live pass/fail behavior — it consumes the same `open_positions_usd_direction` dict, and true FX/equity notional is naturally many times larger than the old buggy numbers it was implicitly calibrated against. This is exactly the kind of "consequential choice that cannot be inferred" the brief tells me to ask about rather than guess — asked via `AskUserQuestion`; user chose: **switch the gate to aggregate risk-at-stop, not notional** (matches how every other limit in `governor.py` is already expressed — 1–3% per trade, 1.5% daily, 4% weekly — so a %-of-NAV cap stays meaningful regardless of instrument leverage, unlike notional).

**Implementation**:
- `src/broker/alpaca.py`: new `get_equity_stop_price()` (equity bracket stop leg, `type="stop"` — distinct from crypto's existing `get_crypto_stop_price()`, `type="stop_limit"`) and `get_position_stop_price()` (asset-class dispatch). OANDA needed no new broker method — `open_trades()` (already existed, from Phase D2) already returns each trade's live `stopLossOrder` inline.
- `src/run_loop.py`: new `compute_correlated_stop_risk()` — aggregates USD risk-at-stop per same-USD-direction bucket. A bucket containing any position with **no live protective stop** is reported as `float('inf')` (fails closed — the gate rejects rather than silently treating unbounded risk as zero). Computed once per cycle per broker (same place `compute_exposure` already runs), threaded through `BrokerCycleContext` → `evaluate_pair` → `_evaluate_one_horizon` → `risk_governor.evaluate()`.
- `src/risk/governor.py`: `evaluate()`'s correlation gate now compares `open_positions_stop_risk_usd` (new, optional, defaults to empty) against `MAX_CORRELATED_EXPOSURE_PCT`, instead of notional. `MAX_EQUITY_CRYPTO_NOTIONAL_PCT`'s own headroom check is untouched — it's still correctly notional-based (that's the right unit for "no leverage"), and it's now actually fed correct numbers per sub-finding 1.

**PaperBroker (pure-forex-paper mode) is explicitly excluded** — it simulates fills against its own DB ledger with no real broker-side protective order to query (unlike OANDA demo / Alpaca paper, both real broker accounts). `compute_correlated_stop_risk` is skipped for it; the correlation gate falls back to its old "0 risk" default for that mode only, same practical status it always had there.

**Tests**: `tests/test_risk_governor_correlation.py` (4 tests, via an isolated in-memory SQLite engine — never the real production DB, this system has live `--auto-execute` trading) + `tests/test_compute_correlated_stop_risk.py` (7 tests, fake broker objects, no network) — cap breach, cap compliance, unprotected-position fail-closed, multi-trade aggregation, no-usd-leg exclusion, mixed protected/unprotected bucket.

### Remaining (NOT done — explicitly scoped out of this pass, tracked for continuation)

- **Candidate's own contribution not yet included in the correlation check.** The gate only evaluates *existing* open positions' risk-at-stop; it does not yet add the trade currently being evaluated. The brief's own T-test doesn't cover this specifically, but the P0-01 prose does ("Evaluate the portfolio after the proposed order"). Needs either a gate-order change (compute size before the correlation check) or a two-pass evaluation.
- **Atomic reservation is NOT built.** "Simultaneous candidates cannot exceed the configured portfolio budget" (P0-01's own acceptance criterion) requires a real reservation mechanism (claim capacity before submission, release on fill/reject/cancel/timeout) — this is a bigger, separate piece of work (likely shares infrastructure with P0-03's "one authoritative submission path" and its durable intent claim). Not attempted this pass.
- **Multi-horizon double-counting** ("coordinate multiple horizons so one market view cannot accidentally consume the same risk budget several times") — not addressed. `_evaluate_one_horizon` runs once per configured horizon per pair per cycle, each independently calling `risk_governor.evaluate()` against the SAME `stop_risk`/`exposure` snapshot (computed once per cycle) — so two horizons on the same pair, both wanting to trade the same direction, would each see the other's un-updated state and could both pass the correlation gate in the same cycle. Real gap, not yet fixed.
- **Concentration cap value (5%) is a judgment call, not derived from data** — flagged as such in the code comment; worth revisiting once real risk-at-stop numbers are observed live (currently unverifiable — the real broker accounts have no populated stop data to check this against without deploying, which is out of scope here).

---

## P0-02 through P0-05, P1-01 through P1-06, UX-01 through UX-10, research tracks — **NOT STARTED**

Not investigated or implemented this session beyond the reading already done to scope P0-01 (which touches the same `src/risk/governor.py` and `src/run_loop.py` files P0-02/P0-03 also target — worth re-reading fresh rather than assuming no interaction). Continuing in priority order per the brief's own Phase B ("P0-01 through P0-05; basic risk/order status UI") is the natural next step:

- **P0-02** (persistent emergency stop, coherent loss budgets) — `src/risk/governor.py`'s `set_kill_switch`/`_get_or_init_day_state` already exist and were read while working on P0-01; the "tied to calendar day, resets" finding was not independently re-verified this pass.
- **P0-03** (one authoritative submission path) — shares infrastructure with P0-01's un-built atomic reservation (see above).
- **P0-04** (broker reconciliation, protective-order verification) — directly related to this session's "unprotected position -> inf" fail-closed design in the correlation gate; that's a *symptom detector*, not the fix P0-04 actually asks for (durable follow-up, recovery, honest crypto-target-support disclosure).
- **P0-05** (auth/session hardening) — untouched.
- **P1-xx, UX-xx, research tracks** — untouched.

---

## Verification evidence (this session)

- `git diff --stat` scoped to: `src/risk/governor.py`, `src/run_loop.py`, `src/broker/alpaca.py`, plus 3 new test files, plus this tracker. Nothing in dashboard/auth/other broker files touched.
- Full test suite: **40/40 passing** (18 pre-existing + 22 new), including the pre-existing dashboard smoke test (verified individually at 43.7s to rule out a false failure from resource contention when run alongside the rest of the suite).
- No real broker calls, no real orders, no production DB access, no live risk-setting changes — all verification via fake broker objects or an isolated in-memory SQLite engine.
- No currently-open trades were touched, inspected for closure, or otherwise acted on by this work — it changes code that will affect *future* risk-governor decisions on the next live cycle, not anything already open.

## Continuation state

Next slice should be: (1) either close P0-01's two remaining gaps (candidate's-own-contribution, multi-horizon double-counting) since they're natural extensions of what's already built, or (2) move to P0-02 (kill-switch persistence) per the brief's own phase ordering — both are reasonable; P0-02 is probably higher-value since it's a distinct, self-contained, well-specified piece or work. Branch `hardening/p0-order-safety` is pushed to GitHub (not merged) for review.

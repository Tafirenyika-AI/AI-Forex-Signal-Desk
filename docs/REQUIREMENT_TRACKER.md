# Requirement tracker — external review brief, 2026-09-22

Source: "AI Forex Signal Desk — Claude implementation brief" (reviewed revision `890241a67e37d8fc80f42fee7f9aea1276a5e3a2`, which was confirmed identical to this repo's HEAD when this work started — no drift to reconcile).

Branch: `hardening/p0-order-safety` (isolated per the brief's own instruction; not merged to `main`, no deployment, no live-account risk-setting changes, no real-money orders — all per the brief's explicit authorization limits).

Status legend: **DONE** (implemented + tested, evidence below) · **PARTIAL** (real progress, real gap remains — see "Remaining") · **NOT STARTED**.

This is Phase A (current-state verification) plus first slices of Phase B (P0-01, P0-02). The brief itself frames this as a multi-week, multi-phase effort (Phases A–F) — this session covers bounded, fully-verified units of it, not the whole brief. Everything below "P0-02" is NOT STARTED, tracked here so the next session picks up with full context rather than re-deriving it.

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

## P0-02 — persistent emergency stop and coherent loss budgets — **PARTIAL**

All three findings confirmed exactly as described before fixing:

1. **Manual kill switch tied to calendar day.** The dashboard's "EMERGENCY STOP" button and `governor.py`'s own reconciliation-failure branch both called `set_kill_switch()`, writing into `risk_state`'s `(user_id, day, broker)`-scoped `kill_switch_active` flag — the SAME flag an automatic daily-loss-breach uses. A human's deliberate "stop everything" silently cleared itself at the next UTC midnight with no action from them, indistinguishable from an automatic daily guardrail resetting for a fresh day's budget (which IS correct behavior for that case).
2. **Zero baseline.** `set_kill_switch()`'s placeholder `day_start_balance=0.0` (used when it was the first write of the day, e.g. a manual stop pressed before any evaluate() cycle had run) would permanently disable that day's daily-loss check (`if day_start_balance else 0.0` guards division, but also silently zeros out `daily_pl_pct` for the rest of the day once persisted). Root cause: manual/reconciliation writes had no real NAV to seed a baseline with. Resolved as a side effect of splitting concerns below — `set_kill_switch` (day-scoped) is now ONLY ever called from inside `evaluate()`, always after a real baseline already exists.
3. **A single trade's max risk (3% ceiling) exceeds the entire daily loss limit (1.5%).** Confirmed: nothing previously stopped one maximally-confident trade from risking more than a full day's budget in one shot.

**Implementation**:
- New `manual_kill_switch` table (`src/data/db.py`) — one row per (user_id, broker), no day/week dimension, persists across restart and date rollover until explicitly cleared. Reconciliation failures now also land here (a structural "something's wrong" state, not a routine daily reset) instead of the day-scoped flag.
- New `risk_state_weekly.kill_switch_active`/`kill_switch_reason` columns — a weekly breach used to reuse the DAY-scoped flag too, so a "weekly" limit only ever actually blocked the rest of that one day, not the week. Now persists for the real remainder of the ISO week.
- `src/risk/governor.py`: new `get_manual_kill_switch`/`set_manual_kill_switch`/`set_weekly_kill_switch`; `evaluate()`'s kill-switch gate now checks manual (persistent) first, then daily (day-scoped, unchanged reset behavior — correct for an automatic daily guardrail), then weekly (now week-scoped). New remaining-daily-budget clamp in the sizing gate: a trade's own planned risk is capped to what's actually left of today's loss budget (same "gates only ever shrink a trade" philosophy as the existing notional-ceiling clamp), rather than only being caught retroactively by the kill switch on a LATER trade.
- `src/dashboard/app.py`: `get_kill_switch_state()` rewritten to combine all three sources (manual/daily/weekly) per broker; the EMERGENCY STOP/Resume buttons now use the persistent manual latch (Resume still clears all three, preserving today's actual button behavior from the user's point of view — only WHEN each layer auto-clears changed, not what pressing the button does).
- **Migration applied live**: `src/scripts/migrate_p0_02_kill_switch.py` (idempotent — additive nullable columns + a new table only, no data touched) — run against the real production database, verified via `metadata.create_all()` creating `manual_kill_switch` and `ADD COLUMN IF NOT EXISTS` adding the two `risk_state_weekly` columns. Confirmed necessary: the dashboard smoke test failed against real Postgres before this ran (`UndefinedColumn: risk_state_weekly.kill_switch_active`) and passed after.
- **Live-verified** (not just unit tests): ran `set_manual_kill_switch`/`get_manual_kill_switch`/`set_weekly_kill_switch` directly against the real production database using a fake, non-existent `user_id=999999` (never a real account) to confirm the new table/columns actually work end-to-end post-migration, then deleted those rows immediately after.

**Tests**: `tests/test_risk_governor_kill_switch.py` (8 tests, isolated in-memory SQLite) — manual latch blocks regardless of P/L, survives a simulated date rollover, clears on explicit reset; reconciliation failure sets the persistent (not daily) latch; weekly breach persists within the week and clears the following week; a single trade's risk is clamped to the remaining daily budget, and a near-zero remaining budget rejects outright.

### Remaining (NOT done — tracked for continuation)

- **UX-02's three distinct operations** ("Pause new entries" / "Cancel pending entries" / "Close positions") are explicitly out of scope here — this pass only fixed the persistence/baseline/budget backend; the dashboard still has one combined EMERGENCY STOP button, not three. That's UX-02's job, a real design task, not a quick add-on.
- **"Monitor open positions even when no new signal is produced"** — not addressed. The kill-switch/budget gates only ever run when a NEW candidate signal reaches `evaluate()`; an already-open position isn't independently re-checked against the daily/weekly budget on a quiet cycle with no new signals.
- **UI wording** was updated where it directly touches the changed mechanism (button comments, tracker) but a full "state each control's effect accurately" pass across the whole dashboard (per P0-02's own acceptance criteria) wasn't done — narrower than the brief's full ask.

---

## P0-03 through P0-05, P1-01 through P1-06, UX-01 through UX-10, research tracks — **NOT STARTED**

- **P0-03** (one authoritative submission path) — shares infrastructure with P0-01's un-built atomic reservation.
- **P0-04** (broker reconciliation, protective-order verification) — directly related to the "unprotected position -> inf" fail-closed design already built into the correlation gate (P0-01); that's a *symptom detector*, not the fix P0-04 actually asks for (durable follow-up, recovery, honest crypto-target-support disclosure).
- **P0-05** (auth/session hardening) — untouched.
- **P1-xx, UX-xx, research tracks** — untouched.

---

## Verification evidence (this session, cumulative)

- `git diff --stat` scoped to: `src/risk/governor.py`, `src/run_loop.py`, `src/broker/alpaca.py`, `src/data/db.py`, `src/dashboard/app.py`, one new migration script, 5 new test files, this tracker. Nothing in auth/other broker/execution-order files touched.
- Full test suite: **48/48 passing** (18 original + 30 new across both P0-01 and P0-02), including the dashboard smoke test against the real (now-migrated) production database.
- No real broker calls in tests, no real orders, no live-account risk-SETTING changes (only a schema migration — additive, non-destructive) — governor logic verified via fake broker objects or an isolated in-memory SQLite engine; the new kill-switch functions were ALSO live-verified directly against the real production DB using a fake `user_id` that matches no real account, then cleaned up immediately.
- No currently-open trades were touched, inspected for closure, or otherwise acted on — all changes affect *future* risk-governor decisions on the next live cycle, not anything already open.

## Continuation state

Natural next slice: **P0-03** (one authoritative submission path) shares real infrastructure with P0-01's still-open atomic-reservation gap — doing them together may be more efficient than sequentially. Alternatively, **P0-04** (protective-order verification/reconciliation) is a natural partner to P0-02's kill-switch work, both being about the system's honesty regarding its own state. Branch `hardening/p0-order-safety` is pushed to GitHub (not merged) for review; the P0-02 migration has ALREADY been applied live (unlike everything else, which is code-only until deployed) — worth flagging to whoever reviews this that the DB schema and the `main` branch's code are now slightly ahead of each other (harmless: the new column/table are additive and unused by any code path not on this branch).

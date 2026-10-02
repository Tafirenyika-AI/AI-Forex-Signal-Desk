# Equity Intelligence V2 — Implementation Log

Source brief: "AI TRADING DESK — EQUITY INTELLIGENCE V2" (pasted 2026-10-01). Companion to `docs/EQUITY_V2_AUDIT.md` (Phase 0). Each phase gets its own dated section below, in the format every prior section of this project's work has used (see `docs/REQUIREMENT_TRACKER.md` for the external-review-brief precedent this follows).

Per-phase entries record: files changed, schema changes, tests added, tests passed/failed, known limitations, execution-impact assessment (did this phase touch anything capable of creating/modifying/closing a broker order — see audit section 12/13 for the enumerated list to check against).

---

## Phase 0 — Safe Repository Audit — **DONE**

2026-10-01. Produced `docs/EQUITY_V2_AUDIT.md` (14 sections, all requested). Captured required pre-implementation read-only broker snapshot (`docs/EQUITY_V2_PRE_IMPLEMENTATION_SNAPSHOT.json`) before any inspection began — real state at capture: zero open positions on either broker (the one position open earlier that day, an MSFT short, independently confirmed to have closed via its own pre-existing protective stop order, not by anything in this audit).

**Files changed**: none (read-only phase, as instructed). Two new files created: `docs/EQUITY_V2_AUDIT.md`, `docs/EQUITY_V2_PRE_IMPLEMENTATION_SNAPSHOT.json`, this log.

**Schema changes**: none.

**Tests**: none added (no code changed). Confirmed no change to `src/config.py`'s live-trading refusal, no order placed, no position touched.

**Execution-impact assessment**: zero. Nothing in this phase is capable of reaching section 12/13's enumerated order-mutating paths — every action taken was a read (broker state queries, source file reads) or a new-file write under `docs/`.

**Known limitations carried forward** (full detail in the audit): no `EQUITY_V2_ACTIVATED_AT` boundary exists yet (recommended as a new singleton table, not yet created); the backtest engine's historical confidence numbers carry an unquantified dilution effect from the same bug fixed live today; `active_trading_users()`'s OANDA INNER JOIN remains a real structural risk for a future equity-only user.

**Next**: awaiting direction on which phase to pick up next — the brief itself frames this as a large, multi-week, phase-by-phase effort ("work incrementally... do not make one giant rewrite"), consistent with how this project's other large brief (the external code-review brief, see `docs/REQUIREMENT_TRACKER.md`) was handled.

---

## Phase 1 — Broker Reconciliation Engine — **DONE**

2026-10-01. Built `src/reconciliation/alpaca.py` — the brief's four required primitives (account/positions/open-orders/historical-orders, `ReconciliationIssue` records with the exact requested fields, VERIFIED/WARNING/UNRESOLVED/CRITICAL severities, the explicit INTERNAL-vs-BROKER-VERIFIED P&L distinction) plus the four checks it made possible: position reconciliation, protective-order coverage, unexplained-broker-order detection, and P&L reconciliation.

**Files changed**: `src/reconciliation/__init__.py` (new), `src/reconciliation/alpaca.py` (new, ~300 lines), `src/data/db.py` (+1 table), `src/dashboard/app.py` (consolidated a duplicated starting-deposit constant to import from the new module instead of keeping its own copy), `tests/test_reconciliation_alpaca.py` (new, 18 tests).

**Schema changes**: one new additive table, `reconciliation_issues` (persists every `ReconciliationIssue` a run produces — `severity`, `symbol`, `issue_type`, `broker_value`, `internal_value`, `detected_at`, `description`, `suggested_investigation`, plus `resolved_at`/`resolved_note` for a human to close one out later; never written by the reconciliation run re-checking itself). Also gave the existing, previously-dormant `positions_snapshots` table (defined in the schema since early in this project, never once written to by any code path) its first real writer — one row per reconciliation run, exactly the point-in-time account snapshot it was evidently built for.

**Real finding on the first live run** (not a hypothetical — ran it against the real production account immediately after writing it, per this project's own verification standard): position reconciliation flagged BTC/USD as CRITICAL — broker reports exactly 0, but FIFO-walking Alpaca's own order history reconstructed a nonzero 5.181e-6 BTC "still open." Investigated rather than loosened the check blindly: summed buy-minus-sell across this account's 7 real BTC/USD round trips by hand and got exactly 5.181e-6 (matching to 9 decimal places) — confirmed via a fresh live `/positions` call that the broker's own number is genuinely, exactly flat, not a display-rounded dust entry. Root cause: Alpaca deducts crypto trading fees from the position itself (already documented in `src/outcomes/alpaca_tracker.py`'s own module docstring from earlier the same day), so a round trip's sell leg is always a little less than its buy leg — real fee consumption, not drift or a bug. Fixed with an asset-class-aware tolerance (`asset_class_for`) rather than a single shared number, which would have either false-alarmed on routine crypto fee dust or masked a real equity mismatch under a too-loose shared threshold. Added two tests locking this in: the exact real 7-round-trip pattern must stay VERIFIED, and an equivalently tiny equity gap must still be CRITICAL (no comparable fee mechanic exists there).

**Second real finding, already known but now permanently tracked**: 13 "unexplained broker order" findings (UNRESOLVED), all real — Alpaca order history containing client_order_ids like `test-361e72f4ddcb`, `synctest-crypto-944826ea`, `trailing-verify-crypto-test` from this and earlier sessions' own ad-hoc live-verification scripts (see memory: Phase D2's "verification-script mishap" note), plus the one genuinely unexplained NVDA close from `docs/EQUITY_V2_AUDIT.md` section 12. None of these are a surprise, but none were discoverable in one place before this — now they're queryable (`reconciliation_issues` where `issue_type='unexplained_broker_order'`) instead of living only in scattered memory files and one-off investigations.

**P&L reconciliation threshold bug caught by the test-first discipline, not by inspection**: the original implementation required the dollar-amount AND percent-of-NAV thresholds to both be exceeded before flagging WARNING/CRITICAL — a test with a realistic small-account gap ($300 on $100,500 NAV, comfortably over the $100 floor but under the 0.5%-of-NAV floor) proved this would silently pass as VERIFIED. Changed to OR: either threshold alone is enough, since a small-dollar gap can still be a large fraction of a small account, and a large-dollar gap can still be a tiny fraction of a large one. Verified via the standard revert-and-confirm-the-test-fails cycle before restoring.

**Tests**: `tests/test_reconciliation_alpaca.py`, 18 tests — pure-function coverage for `_net_position`/`_is_known_client_order_id`, each of the four checks' VERIFIED/WARNING/CRITICAL/UNRESOLVED paths including the two real-pattern cases above, and one full end-to-end test of `reconcile()` itself against a fake `AlpacaBroker` (`_request`/`account_state`/`positions` monkeypatched, same pattern `tests/test_alpaca_crypto_protection.py` established) + an isolated in-memory SQLite engine, confirming both the returned `ReconciliationReport` and the persisted `reconciliation_issues`/`positions_snapshots` rows. Full suite: 146/146 passing.

**Execution-impact assessment**: zero. Every broker call in this module is a GET — confirmed by reading every call site (`account_state()`, `positions()`, `_request(..., "GET", ...)` twice for open/all orders). No `place_order`, `cancel_order`, or `modify_stop_loss` call exists anywhere in the file. Ran twice against the real production account as part of verification; both runs were pure reads plus the intended new-table writes (`reconciliation_issues`, `positions_snapshots`) — no order placed, no position touched, confirmed by the position-reconciliation check itself reporting all three real symbols as matched both times.

**Known limitations, disclosed not hidden**:
- The "executed trade_intents have a matching broker fill" cross-check from the original design sketch was not implemented this pass (would need live `get_order()` calls per intent, meaningfully more complex/rate-limit-sensitive than the four checks that shipped) — scoped out, not forgotten.
- `STARTING_DEPOSIT` is still a single hardcoded constant (now in one place instead of two, but still not a real deposit/withdrawal ledger) — same disclosed gap the Phase 0 audit already named.
- No scheduled task runs this yet — it's a library function + a manual entry point today, callable via `python -c` the same way every other one-off verification this session has been. Wiring a recurring schedule (and deciding how `reconciliation_issues` should be surfaced on the dashboard — Phase 16's own ask) is a natural next step, not done here.
- OANDA has no reconciliation module yet — this phase is Alpaca-only, matching the brief's own Phase 1 heading ("Broker Reconciliation Engine... src/reconciliation/alpaca.py").

**Next**: dashboard surfacing of `reconciliation_issues` (part of Phase 16's own ask, but small enough to consider pulling forward), or continue to another phase per the brief's own ordering — awaiting direction.

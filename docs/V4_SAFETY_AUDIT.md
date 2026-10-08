# V4 Phase 0 — Alpaca Open-Position & Protective-Order Safety Audit

Source brief: "AI TRADING DESK V4 — FINAL MASTER IMPLEMENTATION BLUEPRINT" (pasted 2026-10-08). This is Section 2's own mandatory first step, performed before any other V4 work, using a live, read-only query of the real Alpaca account — never assumed from code alone.

## 1. Real account state at time of audit (2026-10-08, ~01:45 UTC)

Queried live via `AlpacaBroker.account_state()` / `.positions()` / direct `GET /v2/orders`:

- **Account**: NAV $103,325.96, cash balance $440,472.54, unrealized P&L +$1,100.76, margin used $168,573.29, margin available $8,727.94.
- **Open positions (2)**:
  - `AAPL` — **short** 400 shares, avg entry $335.7556, unrealized P&L -$137.76, market value -$134,440.
  - `MSFT` — **short** 383 shares, avg entry $532.493733, unrealized P&L +$1,238.52, market value -$202,706.58.
- **Open orders (5, all `order_class=bracket`, `side=buy`, `position_intent=buy_to_close`)** — these are the take-profit legs of the brackets that opened each short tranche: 2 for AAPL (qty 91 @ $331.94, qty 309 @ $330.77), 3 for MSFT (qty 10 @ $522.87, qty 194 @ $523.36, qty 179 @ $524.24). Together they sum exactly to the open position sizes (91+309=400, 10+194+179=383), confirming each short was built from several separate tranches over time, not one single entry.

## 2. Real, currently-active bugs found and fixed during this audit

Both found by directly querying the real account rather than trusting the code — see each one's own "Real bug found live 2026-10-08" comment in the code for full detail. **Fixed, tested, live-verified, and pushed (`2f371c7`) with the user's explicit approval before touching this execution-adjacent code.**

### Bug 1 — real short positions misread as "long"

`src/run_loop.py`'s `_normalized_positions()` assumed Alpaca's `qty` field is always an unsigned magnitude and applied a sign itself from `side` (`net_units = qty if side=="long" else -qty`). Alpaca's real **position**-level `qty` is actually pre-signed (confirmed live: AAPL returned `qty="-400"`, `side="short"`). The old code's `-qty` double-negated an already-negative number back to positive.

**Real consequence**: both AAPL and MSFT were read as `"long"` everywhere this function's output fed:
- `compute_exposure()`'s correlation-gate bucketing (showed `equity_long: $338,247` instead of `equity_short`).
- The Equity V2 Phase 14 `no_pyramid_same_symbol` gate (`compute_open_directions_by_instrument`) — meaning that protection, built and tested 6 days earlier specifically to stop repeated same-direction order pyramiding, was **not actually covering these two real positions**.

**Fix**: take `abs(qty)` first, then apply the sign from `side` explicitly — correct regardless of whether a future Alpaca response is pre-signed or not.

### Bug 2 — false "unprotected position" / infinite-risk alerts

`AlpacaBroker.get_equity_stop_price()` queried orders with `status="open"`. Confirmed live that Alpaca reports a real, live, protective stop order's status as `"held"` (not `"open"`/`"new"`) whenever its sibling take-profit leg is still resting in the same OTO/OCO group — exactly the current state of both real positions' brackets. The `status="open"` filter silently excluded these real orders.

**Real consequence**: the risk governor's correlation gate (`compute_correlated_stop_risk`) treated both genuinely-protected positions as `float('inf')` unbounded risk. The **same bug independently existed** in `src/reconciliation/alpaca.py`'s `_check_protective_orders` (Phase 1 of the Equity V2 effort, same `status="open"` fetch) — generating a false `CRITICAL unprotected_position` finding for both real positions on **every 30-minute scheduled reconciliation run**.

**Fix**: both call sites now exclude only genuinely terminal order statuses (`filled`/`canceled`/`expired`/`rejected`/`replaced`/`stopped`/`done_for_day`) rather than trusting Alpaca's "open" bucket. The exclusion list is a shared module-level constant (`ALPACA_TERMINAL_ORDER_STATUSES` in `src/broker/alpaca.py`) so the two sites can't drift apart. Reconciliation's separate `status="open"` fetch was removed entirely (it now reuses the `status="all"` fetch it already made for a different check).

**Verified NOT to need the same fix**: Alpaca's **order**-level `filled_qty` field is genuinely unsigned (confirmed live — a real sell order's `filled_qty` came back as a positive string). `_net_position()` in the reconciliation module derives sign from `side` using `filled_qty`, which is the correct convention for that field — it was never affected by Bug 1's issue, since that's a different API surface with a different sign convention. Documented explicitly in code so a future pass doesn't "fix" something that isn't broken.

**Post-fix live confirmation**: both positions now report direction `"short"` correctly, real bounded risk-at-stop (~$2,442, not infinite), and the reconciliation report shows `VERIFIED protective_order_present` for both instead of the false `CRITICAL` alert.

## 3. Other real findings during this audit (not fixed — informational / pre-existing / lower priority)

- **8 `UNRESOLVED unexplained_broker_order` findings** across AAPL/MSFT (live reconciliation run, post-fix) — real filled orders at the broker whose `client_order_id` doesn't match any convention this system mints. This is the same, already-known category Phase 1 of Equity V2 first surfaced (ad-hoc verification scripts' own test trades, mostly). Not re-investigated in this pass; a reasonable candidate for a closer look under V4 Section 15 (broker-verified performance / reconciliation).
- **Multiple separate bracket tranches per position** (2 for AAPL, 3 for MSFT) confirm the position-sizing/entry pattern already known from the Equity V2 Phase 14 investigation (repeated signals building a position incrementally). With Bug 1 now fixed, the `no_pyramid_same_symbol` gate will actually see these as `"short"` going forward and correctly block further same-direction entries.
- **No `LEGACY_OPEN_POSITION` classification exists** anywhere in the codebase (confirmed by grep — only ever mentioned in `docs/EQUITY_V2_AUDIT.md` as a disclosed, not-yet-built gap). Both currently open positions (and any future ones that predate whichever moment V4's own activation boundary is eventually set) would need this classification once built. **Not built in this Phase 0 pass** — flagged as a required Phase 1/2 item (see `docs/V4_ARCHITECTURE.md`'s gap analysis and implementation plan).
- **`modify_stop_loss()` for equities remains deliberately unimplemented** (raises `NotImplementedError`, confirmed by reading the code) — canceling one leg of an Alpaca bracket cancels the whole bracket (parent + both legs, verified live in an earlier session), so no code path in this system can currently touch an equity position's protective stop without risk of briefly leaving it unprotected. This is a real, existing, deliberate safety constraint, not a gap — V4 must not add equity bracket-stop modification without re-solving this exact problem (e.g. an atomic replace, which Alpaca doesn't offer) first.

## 4. Confirmed-safe existing behaviors (checked directly, not assumed)

- `src/config.py`'s live-trading refusal is intact for both brokers and is the *only* path `Settings()` is ever constructed from (confirmed by grep across `src/`) — see `tests/test_deployment_mode_confirmation.py` (Equity V2 Phase 20) for the automated regression guard already in place.
- `--auto-execute` and `--enable-trailing-stops` both default to off (`action="store_true"`); `--mode` defaults to `"shadow"`.
- `ExecutionService.execute()` is idempotent on `client_order_id` (`ON CONFLICT DO NOTHING`) — confirmed by direct test (`tests/test_execution_idempotency_and_restart.py`, Equity V2 Phase 19) and by this audit's own code read.
- Nothing in `src/decision/fusion.py`, `src/risk/governor.py`'s live gate chain, or `src/execution/service.py` was modified by the Equity V2 effort's shadow-only components (Phase 10 challengers, Phase 13 governor extensions) — confirmed previously by grep, re-confirmed still true by this audit (no changes to those files since).

## 5. Required tests before any further V4 execution-adjacent change

Per the brief's own Section 2 instruction ("Establish tests ensuring the upgrade does not generate unintended broker actions") and Section 22 (deliverable #5):

1. **Already in place** (Equity V2 Phases 14/19/20 + this audit): same-symbol pyramiding protection, order idempotency, restart recovery, live-trading refusal for both brokers, risk-constant regression guards, and now the two fixes above with their own dedicated tests (`tests/test_alpaca_short_position_sign_bug.py`, `tests/test_alpaca_equity_stop_price_status_bug.py`, plus 2 new reconciliation tests).
2. **Still needed before any `LEGACY_OPEN_POSITION` work begins**: a test proving a position that predates the V4 activation boundary is never auto-modified by ANY new V4 code path (challenger-driven order, strategy-driven order, or scheduled restart) — cannot be written until the activation-boundary mechanism itself exists.
3. **Still needed before any new strategy family (Section 6) can submit even a shadow/paper order**: an end-to-end test proving a new strategy's proposed trade still passes through the existing, unmodified risk governor and still respects `MAX_CONCURRENT_POSITIONS`/correlation caps — i.e., a new strategy must be provably unable to bypass the existing gate chain, not just assumed to go through it.
4. **Before enabling `V4_ALLOW_NEW_PAPER_ORDERS=true` for the first time (Phase 7, explicitly gated on human approval per the brief)**: a final pre-flight snapshot test, matching the exact pattern Equity V2's own Phase 0 used (`docs/EQUITY_V2_PRE_IMPLEMENTATION_SNAPSHOT.json`) — capture real account/position/order state immediately before flipping the flag, and a corresponding "nothing unexpected changed" comparison immediately after.

## 6. Scope note

This document covers Section 2's own explicit audit ask. The broader repository architecture assessment, gap analysis against the full V4 spec, prioritized implementation plan, and API/cost survey are in `docs/V4_ARCHITECTURE.md` and `docs/V4_DATA_SOURCES.md`.

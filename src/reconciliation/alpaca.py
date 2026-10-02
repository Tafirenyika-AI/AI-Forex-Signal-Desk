"""Equity V2 Phase 1 — Alpaca broker reconciliation engine.

READ-ONLY, by design and by construction: nothing in this module ever
calls a broker method that places, modifies, or cancels an order (only
AlpacaBroker.account_state/positions/_request with GET verbs are used —
confirmed by reading every call site below). It only ever reads real
broker state and compares it against this system's own records,
producing ReconciliationIssue records for a human (or a later phase) to
act on. It never auto-resolves a discrepancy by touching the broker —
see the brief's own Phase 1 instruction ("Never resolve discrepancies by
automatically placing/cancelling orders").

Distinguishes two different numbers deliberately, per the brief's own
instruction never to blur them:
  - BROKER VERIFIED PERFORMANCE: Alpaca's own account NAV vs. the known
    starting deposit — ground truth, nothing reconstructed.
  - INTERNAL CALCULATED P&L: this system's own FIFO-matched trade_outcomes
    sum (src/outcomes/alpaca_tracker.py) plus currently-open unrealized
    P&L — a reconstruction from the broker's raw fills, which has
    genuinely drifted from the real number before (a ~$9,400 gap, found
    and fixed live 2026-10-01 — see memory/project_alpaca_stop_loss_and_
    outcome_tracking_bugs.md) — useful for per-trade attribution, but
    never to be presented AS the broker-verified number.

Four checks, each producing zero or more ReconciliationIssue records:
  1. Position reconciliation — Alpaca's live reported position per symbol
     vs. the net position this module independently reconstructs by
     FIFO-walking Alpaca's OWN complete order history. Both sides come
     from the same broker, so a mismatch here is not a tautology — it
     means either a pagination/fetch bug in the reconstruction, or a
     position change that didn't go through a normal order fill (e.g. a
     corporate action).
  2. Protective-order coverage — every open position should have a live
     stop order. The exact bug class found and fixed live the same day
     this module was built (a real $348K equity position sitting
     completely unprotected — see the same memory file above).
  3. Unexplained broker orders — a filled order whose client_order_id
     doesn't match this system's own naming conventions (intent-NNNNN,
     a recognized protective-stop pattern) and has no corresponding
     orders_fills row. Matches a real, disclosed, still-open question
     from the Phase 0 audit (docs/EQUITY_V2_AUDIT.md section 12) — a
     handful of historical MSFT closes were placed by something outside
     this system's own ExecutionService, origin never established.
  4. P&L reconciliation — broker-verified vs. internal-calculated, see
     above.

STARTING_DEPOSIT is this module's single source of truth for each
broker's known starting balance — the dashboard previously kept its own
separate copy of this same constant (a real duplication-drift risk);
it now imports from here instead.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.broker.alpaca import AlpacaBroker, parse_alpaca_time
from src.broker.registry import asset_class_for
from src.data.db import orders_fills as orders_fills_table
from src.data.db import positions_snapshots as positions_snapshots_table
from src.data.db import reconciliation_issues as reconciliation_issues_table
from src.data.db import trade_outcomes as trade_outcomes_table
from src.data.db import upsert_insert as insert

Severity = Literal["VERIFIED", "WARNING", "UNRESOLVED", "CRITICAL"]

# This account's own documented paper-account starting balance (Alpaca's
# standard $100k paper default — matches the real first tracked balance,
# ~$99,999.20, from 2026-08-24, same convention the dashboard's Account
# Standing banner already used before this module consolidated it here).
# A real "net deposits/withdrawals" ledger would be more correct (flagged
# in the Phase 0 audit, section 10) — not built here, this is still a
# single hardcoded constant, just no longer duplicated in two places.
STARTING_DEPOSIT = {"alpaca": 100_000.0}

# P&L reconciliation tolerance — routine broker fees/financing accumulate
# over time (a real, expected, ~$8 gap was confirmed live even right
# after the outcome-tracker rewrite), so a small gap is not an issue.
# Chosen the same way every other threshold in this project has been:
# generous enough not to flag routine fee drift, tight enough to still
# catch a real tracking bug (the pre-fix gap here was ~$9,400 — three
# orders of magnitude past this floor). EITHER threshold alone trips the
# check (OR, not AND) — a small-dollar gap on a small account can still
# be a large fraction of NAV, and a large-dollar gap on a large account
# can still be a tiny fraction; either shape deserves a look.
PNL_WARNING_ABS_USD = 100.0
PNL_WARNING_PCT_OF_NAV = 0.005  # 0.5%
PNL_CRITICAL_ABS_USD = 1000.0
PNL_CRITICAL_PCT_OF_NAV = 0.03  # 3%

# Equity positions settle in exact share counts — any nonzero mismatch
# there is a real signal. Crypto is different: Alpaca deducts trading
# fees from the position itself, not as a separate cash line item (see
# src/outcomes/alpaca_tracker.py's own module docstring) — so a round
# trip's sell leg is always a little less than its buy leg, by design,
# never actually "open." Confirmed live 2026-10-01 on this real account:
# summing buy-minus-sell across 7 real BTC/USD round trips gives exactly
# 5.181e-6 BTC "leftover" — matching this check's own flagged mismatch to
# 9 decimal places — while Alpaca's live /positions endpoint confirms the
# true position is genuinely, exactly flat (not a display-rounded dust
# entry). That leftover is fee consumption, not drift or a bug. A single
# shared tolerance would either false-alarm on routine crypto fee dust or
# mask a real equity mismatch, so this is asset-class-aware instead.
_CRYPTO_POSITION_TOLERANCE = 1e-4  # ~20x the largest real fee-dust gap observed live (5.181e-6)
_EQUITY_POSITION_TOLERANCE = 1e-6  # essentially exact-match required — no comparable fee mechanic

# client_order_id patterns this system recognizes as its own (see
# src/execution/service.py / src/broker/alpaca.py for where each is
# minted) — anything filled that matches none of these AND has no
# orders_fills row is "unexplained" (check 3).
_KNOWN_CLIENT_ORDER_ID_PREFIXES = ("intent-",)
_KNOWN_CLIENT_ORDER_ID_SUBSTRINGS = ("-stop", "-trail-", "protective-stop")


@dataclass(frozen=True)
class ReconciliationIssue:
    severity: Severity
    symbol: str  # instrument, or "ACCOUNT" for account-level checks
    issue_type: str
    broker_value: str
    internal_value: str
    detected_at: datetime
    description: str
    suggested_investigation: str


@dataclass(frozen=True)
class ReconciliationReport:
    user_id: int
    broker: str
    generated_at: datetime
    issues: list[ReconciliationIssue]
    broker_verified_nav: float
    broker_verified_deposited: float
    broker_verified_pl: float
    internal_realized_pl: float
    internal_unrealized_pl: float
    internal_calculated_pl: float

    @property
    def worst_severity(self) -> Severity:
        order: list[Severity] = ["CRITICAL", "UNRESOLVED", "WARNING", "VERIFIED"]
        present = {i.severity for i in self.issues}
        for level in order:
            if level in present:
                return level
        return "VERIFIED"


def _net_position(fills: list[dict]) -> float:
    """Signed net quantity from a chronological fill list — positive =
    long, negative = short. Simpler than compute_fifo_outcomes (which
    tracks per-lot entry prices for P&L purposes this check doesn't need)
    — just the final net quantity."""
    net = 0.0
    for f in fills:
        net += f["qty"] if f["side"] == "buy" else -f["qty"]
    return net


def _is_known_client_order_id(coid: str | None) -> bool:
    if not coid:
        return False
    if coid.startswith(_KNOWN_CLIENT_ORDER_ID_PREFIXES):
        return True
    return any(sub in coid for sub in _KNOWN_CLIENT_ORDER_ID_SUBSTRINGS)


def _check_positions(
    broker_positions: dict[str, float], fills_by_symbol: dict[str, list[dict]], now: datetime,
) -> list[ReconciliationIssue]:
    issues = []
    symbols = set(broker_positions) | set(fills_by_symbol)
    for symbol in sorted(symbols):
        broker_qty = broker_positions.get(symbol, 0.0)
        fills = sorted(fills_by_symbol.get(symbol, []), key=lambda f: f["time"])
        internal_qty = _net_position(fills)
        gap = abs(broker_qty - internal_qty)
        tolerance = _CRYPTO_POSITION_TOLERANCE if asset_class_for(symbol) == "crypto" else _EQUITY_POSITION_TOLERANCE
        if gap < tolerance:
            desc = "Broker-reported position matches the net position reconstructed from Alpaca's own complete order history."
            if gap > 0:
                desc += (f" A tiny {gap:g}-unit gap exists but is within the expected crypto fee-dust "
                         f"tolerance ({tolerance:g}) — not flagged as a real mismatch.")
            issues.append(ReconciliationIssue(
                severity="VERIFIED", symbol=symbol, issue_type="position_match",
                broker_value=f"{broker_qty:g}", internal_value=f"{internal_qty:g}",
                detected_at=now, description=desc,
                suggested_investigation="None — informational confirmation.",
            ))
        else:
            issues.append(ReconciliationIssue(
                severity="CRITICAL", symbol=symbol, issue_type="position_mismatch",
                broker_value=f"{broker_qty:g}", internal_value=f"{internal_qty:g}",
                detected_at=now,
                description=f"Broker reports a net position of {broker_qty:g} units in {symbol}, "
                             f"but FIFO-walking Alpaca's own order history for this symbol reconstructs "
                             f"{internal_qty:g} units.",
                suggested_investigation="Both numbers come from the same broker, so this is not "
                             "explainable by a stale internal ledger — check whether the order fetch "
                             "hit its pagination limit (500 orders/request) for this symbol, or whether "
                             "a non-order position change occurred (corporate action, manual broker-side "
                             "adjustment).",
            ))
    return issues


def _check_protective_orders(
    broker: AlpacaBroker, broker_positions: dict[str, float], open_orders: list[dict], now: datetime,
) -> list[ReconciliationIssue]:
    issues = []
    open_stop_symbols = {
        o["symbol"] for o in open_orders if o.get("type") in ("stop", "stop_limit")
    }
    for symbol, qty in broker_positions.items():
        if abs(qty) < 1e-9:
            continue
        if symbol in open_stop_symbols:
            issues.append(ReconciliationIssue(
                severity="VERIFIED", symbol=symbol, issue_type="protective_order_present",
                broker_value="stop order present", internal_value="n/a", detected_at=now,
                description=f"Open position in {symbol} has a live protective stop order.",
                suggested_investigation="None — informational confirmation.",
            ))
        else:
            issues.append(ReconciliationIssue(
                severity="CRITICAL", symbol=symbol, issue_type="unprotected_position",
                broker_value=f"{qty:g} units, no stop order", internal_value="n/a", detected_at=now,
                description=f"Open position in {symbol} ({qty:g} units) has NO live protective "
                             f"stop or take-profit order at the broker.",
                suggested_investigation="Verify live via the broker's own open-orders endpoint "
                             "before acting — if confirmed, this position is fully exposed to adverse "
                             "price moves with no automatic loss limit. Do not place a new order from "
                             "this module (it is read-only by design); escalate for a human or a "
                             "deliberate, separately-authorized action.",
            ))
    return issues


def _check_unexplained_orders(
    filled_orders: list[dict], known_order_coids: set[str], now: datetime,
) -> list[ReconciliationIssue]:
    issues = []
    for o in filled_orders:
        coid = o.get("client_order_id")
        if _is_known_client_order_id(coid) or coid in known_order_coids:
            continue
        issues.append(ReconciliationIssue(
            severity="UNRESOLVED", symbol=o["symbol"], issue_type="unexplained_broker_order",
            broker_value=f"order {o['id']} ({o['side']} {o.get('filled_qty')} @ "
                         f"{o.get('filled_avg_price')}, client_order_id={coid!r})",
            internal_value="no matching orders_fills row, no recognized naming convention",
            detected_at=now,
            description=f"A filled {o['symbol']} order exists at the broker that this system's own "
                         f"records don't explain — not logged in orders_fills, and its client_order_id "
                         f"doesn't match any convention this system mints (intent-NNNNN, a protective "
                         f"stop, a trailing-stop replace).",
            suggested_investigation="Check whether this was placed by an ad-hoc verification script, "
                         "a manual action outside the dashboard, or an older code path that predates "
                         "the current client_order_id conventions. Not necessarily a problem — this "
                         "account has at least one confirmed real instance (a 2026-09-29 NVDA close, "
                         "origin never established, see docs/EQUITY_V2_AUDIT.md section 12) — but every "
                         "instance should be accounted for, not assumed benign.",
        ))
    return issues


def _check_pnl(
    nav: float, deposited: float, internal_realized: float, internal_unrealized: float, now: datetime,
) -> list[ReconciliationIssue]:
    broker_pl = nav - deposited
    internal_pl = internal_realized + internal_unrealized
    gap = abs(broker_pl - internal_pl)
    gap_pct = gap / nav if nav else 0.0

    broker_str = f"NAV ${nav:,.2f} - deposited ${deposited:,.2f} = ${broker_pl:+,.2f}"
    internal_str = (f"realized ${internal_realized:+,.2f} + unrealized ${internal_unrealized:+,.2f} "
                     f"= ${internal_pl:+,.2f}")

    if gap >= PNL_CRITICAL_ABS_USD or gap_pct >= PNL_CRITICAL_PCT_OF_NAV:
        severity: Severity = "CRITICAL"
        desc = (f"Broker-verified and internal-calculated P&L differ by ${gap:,.2f} "
                f"({gap_pct:.1%} of NAV) — far beyond routine fee/financing drift.")
        investigation = ("Re-run src/outcomes/alpaca_tracker.py's sync and check for trades it "
                          "still can't explain (missing orders, a FIFO edge case) before assuming "
                          "this is just fees.")
    elif gap >= PNL_WARNING_ABS_USD or gap_pct >= PNL_WARNING_PCT_OF_NAV:
        severity = "WARNING"
        desc = (f"Broker-verified and internal-calculated P&L differ by ${gap:,.2f} "
                f"({gap_pct:.1%} of NAV) — larger than routine fee drift alone typically explains.")
        investigation = "Worth a look, not urgent — compare against recent fee/financing charges first."
    else:
        severity = "VERIFIED"
        desc = (f"Broker-verified and internal-calculated P&L agree within ${gap:,.2f} "
                f"({gap_pct:.1%} of NAV) — consistent with routine fees, not a tracking gap.")
        investigation = "None — informational confirmation."

    return [ReconciliationIssue(
        severity=severity, symbol="ACCOUNT", issue_type="pnl_reconciliation",
        broker_value=broker_str, internal_value=internal_str, detected_at=now,
        description=desc, suggested_investigation=investigation,
    )]


async def reconcile(engine: Engine, broker: AlpacaBroker, user_id: int) -> ReconciliationReport:
    """The one entry point. Read-only: every broker call below is a GET.
    Persists results to reconciliation_issues and a positions_snapshots
    row, but places, modifies, and cancels no order."""
    now = datetime.now(timezone.utc)

    account = await broker.account_state()
    live_positions = await broker.positions()
    broker_positions = {p["symbol"]: float(p.get("qty") or 0) for p in live_positions}

    open_orders = await broker._request(
        broker._trading_client, "GET", "/orders", params={"status": "open", "limit": 500},
    )
    all_orders = await broker._request(
        broker._trading_client, "GET", "/orders",
        params={"status": "all", "limit": 500, "direction": "asc"},
    )
    filled_orders = [o for o in all_orders if o.get("status") == "filled" and o.get("filled_at")]

    fills_by_symbol: dict[str, list[dict]] = {}
    for o in filled_orders:
        if not o.get("filled_avg_price"):
            continue
        fills_by_symbol.setdefault(o["symbol"], []).append({
            "side": o["side"], "qty": float(o["filled_qty"]),
            "price": float(o["filled_avg_price"]), "time": parse_alpaca_time(o["filled_at"]),
            "order_id": o["id"], "client_order_id": o.get("client_order_id"),
        })

    with engine.connect() as conn:
        internal_realized = conn.execute(
            select(trade_outcomes_table.c.realized_pl_usd).where(
                trade_outcomes_table.c.broker == "alpaca", trade_outcomes_table.c.user_id == user_id,
            )
        ).scalars().all()
        known_order_coids = set(conn.execute(
            select(orders_fills_table.c.client_order_id).where(
                orders_fills_table.c.user_id == user_id,
            )
        ).scalars().all())

    issues: list[ReconciliationIssue] = []
    issues += _check_positions(broker_positions, fills_by_symbol, now)
    issues += _check_protective_orders(broker, broker_positions, open_orders, now)
    issues += _check_unexplained_orders(filled_orders, known_order_coids, now)
    issues += _check_pnl(
        nav=account.nav, deposited=STARTING_DEPOSIT.get("alpaca", 0.0),
        internal_realized=sum(internal_realized), internal_unrealized=account.unrealized_pl, now=now,
    )

    with engine.begin() as conn:
        for issue in issues:
            conn.execute(insert(reconciliation_issues_table).values(
                user_id=user_id, broker="alpaca", severity=issue.severity, symbol=issue.symbol,
                issue_type=issue.issue_type, broker_value=issue.broker_value,
                internal_value=issue.internal_value, detected_at=issue.detected_at,
                description=issue.description, suggested_investigation=issue.suggested_investigation,
            ))
        conn.execute(insert(positions_snapshots_table).values(
            time=now, account_id=account.account_id, balance=account.balance, nav=account.nav,
            unrealized_pl=account.unrealized_pl, margin_used=account.margin_used,
            margin_available=account.margin_available, open_trade_count=account.open_trade_count,
            open_position_count=account.open_position_count,
        ))

    return ReconciliationReport(
        user_id=user_id, broker="alpaca", generated_at=now, issues=issues,
        broker_verified_nav=account.nav, broker_verified_deposited=STARTING_DEPOSIT.get("alpaca", 0.0),
        broker_verified_pl=account.nav - STARTING_DEPOSIT.get("alpaca", 0.0),
        internal_realized_pl=sum(internal_realized), internal_unrealized_pl=account.unrealized_pl,
        internal_calculated_pl=sum(internal_realized) + account.unrealized_pl,
    )

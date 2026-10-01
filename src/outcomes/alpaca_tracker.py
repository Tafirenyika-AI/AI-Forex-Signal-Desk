"""Links closed Alpaca trades back to the trade_intent that caused them —
the Alpaca counterpart to src/outcomes/tracker.py (OANDA), same purpose
(makes "train the model on results" possible: without this, orders_fills
tells you a trade happened and predictions tells you what the model
thought, but nothing connects either to what actually happened to the
money).

Real bug found live 2026-10-01: the previous version only recognized a
position as "closed" when its ORIGINAL bracket order's own stop-loss/
take-profit leg filled (equities), or a dedicated "{entry}-stop" order
filled (crypto) — matching src/broker/alpaca.py's entry mechanics. But
Alpaca equity brackets used "day" time_in_force (a separate real bug,
fixed the same day — see place_order's own comment), so their protective
legs silently expired at market close, after which a position could only
ever be closed by some LATER, independent order — which this tracker had
no way to recognize as belonging to the same position at all. Confirmed
live: a real +$8,900 NVDA round trip (bought 2026-09-10, sold 2026-09-29
via an unrelated later order) was completely invisible in trade_outcomes,
along with several BTC/USD scalps closed the same way — undercounting
real realized P&L by thousands of dollars.

This version instead FIFO-matches Alpaca's own COMPLETE filled-order
history per instrument — the broker's own fill ledger is unconditional
ground truth for what actually happened to the money, regardless of
whether a close came from a bracket leg, a dedicated stop order, a fresh
signal-driven order, or a manual/external one. trade_intent_id is only
ever populated when a fill's own client_order_id (closing or, if exactly
one entry lot was consumed, the single opening one) resolves to a real
authorization — several entries closed by one fill, or a fill with no
recognizable client_order_id at all (e.g. placed outside this system),
honestly stays unlinked rather than guessing, same posture as the OANDA
tracker's own disclosed unresolvable-ledger gaps.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.broker.alpaca import AlpacaBroker, parse_alpaca_time
from src.data.db import authorizations as authorizations_table
from src.data.db import trade_outcomes as trade_outcomes_table
from src.data.db import upsert_insert as insert

# Alpaca crypto quantities carry many decimal places and fees are deducted
# from the position itself (see alpaca.py's module docstring) — this is
# comfortably below any real fee/rounding residue, just enough to treat a
# fully-consumed lot as exactly zero instead of lingering as a dust lot.
_QTY_EPSILON = 1e-9


def _outcome_label(realized_pl: float) -> str:
    if realized_pl > 0:
        return "WIN"
    if realized_pl < 0:
        return "LOSS"
    return "BREAKEVEN"


def compute_fifo_outcomes(fills: list[dict]) -> list[dict]:
    """fills: ONE instrument's filled orders, already sorted chronologically
    — each {side: "buy"/"sell", qty: float, price: float, time: datetime,
    order_id: str, client_order_id: str|None}.

    Returns one record per fill that REDUCED the net open position (a
    "closing" event), FIFO-matched against whichever earlier same-
    direction fills ("lots") it consumed — potentially several lots for
    one closing fill (a position built up over multiple entries, closed
    in one order), or several closing records chained together if a
    position is reduced in increments. A fill that only adds to (or
    opens) the net position produces no record — it becomes a new lot
    instead. A fill that closes the entire existing position AND flips it
    (e.g. closing a short and opening a long in the same order) produces
    one closing record for the matched portion, and the leftover becomes
    a new lot in the new direction — Alpaca itself allows this in a
    single order when qty exceeds the open position.
    """
    lots: deque[dict] = deque()
    net_sign = 0  # +1 net long, -1 net short, 0 flat
    outcomes: list[dict] = []

    for fill in fills:
        fill_sign = 1 if fill["side"] == "buy" else -1
        qty = fill["qty"]

        if net_sign == 0 or fill_sign == net_sign:
            lots.append({
                "qty": qty, "price": fill["price"], "time": fill["time"],
                "client_order_id": fill["client_order_id"],
            })
            net_sign = fill_sign
            continue

        closing_sign = net_sign  # direction of the lots this fill is consuming
        remaining = qty
        matched_qty = 0.0
        weighted_notional = 0.0
        opened_at = None
        lot_client_ids: set[str] = set()

        while remaining > _QTY_EPSILON and lots:
            lot = lots[0]
            take = min(lot["qty"], remaining)
            weighted_notional += take * lot["price"]
            matched_qty += take
            if opened_at is None or lot["time"] < opened_at:
                opened_at = lot["time"]
            if lot["client_order_id"]:
                lot_client_ids.add(lot["client_order_id"])
            lot["qty"] -= take
            remaining -= take
            if lot["qty"] <= _QTY_EPSILON:
                lots.popleft()

        if matched_qty > _QTY_EPSILON:
            avg_entry = weighted_notional / matched_qty
            realized_pl = (fill["price"] - avg_entry) * matched_qty * closing_sign
            outcomes.append({
                "action": "BUY" if closing_sign > 0 else "SELL",
                "units": matched_qty,
                "entry_price": avg_entry,
                "exit_price": fill["price"],
                "opened_at": opened_at,
                "closed_at": fill["time"],
                "realized_pl_usd": realized_pl,
                "outcome": _outcome_label(realized_pl),
                "broker_trade_id": fill["order_id"],
                "closing_client_order_id": fill["client_order_id"],
                "entry_client_order_id": next(iter(lot_client_ids)) if len(lot_client_ids) == 1 else None,
            })

        if remaining > _QTY_EPSILON:
            # Flipped direction in the same fill — leftover opens a new lot.
            lots.append({
                "qty": remaining, "price": fill["price"], "time": fill["time"],
                "client_order_id": fill["client_order_id"],
            })
            net_sign = fill_sign
        elif not lots:
            net_sign = 0

    return outcomes


async def sync_alpaca_outcomes(engine: Engine, broker: AlpacaBroker, user_id: int, execution_mode: str = "demo") -> int:
    """Pulls this account's complete filled-order history directly from
    Alpaca and FIFO-matches it per instrument (see module docstring for
    why this replaced the old bracket-leg-only matching). Returns the
    number of newly-inserted rows (not updates)."""
    now = datetime.now(timezone.utc)
    orders = await broker._request(
        broker._trading_client, "GET", "/orders",
        params={"status": "all", "limit": 500, "direction": "asc"},
    )

    by_symbol: dict[str, list[dict]] = {}
    for o in orders:
        if o.get("status") != "filled" or not o.get("filled_at") or not o.get("filled_avg_price"):
            continue
        by_symbol.setdefault(o["symbol"], []).append({
            "side": o["side"],
            "qty": float(o["filled_qty"]),
            "price": float(o["filled_avg_price"]),
            "time": parse_alpaca_time(o["filled_at"]),
            "order_id": o["id"],
            "client_order_id": o.get("client_order_id"),
        })

    new_count = 0
    with engine.begin() as conn:
        for instrument, fills in by_symbol.items():
            fills.sort(key=lambda f: f["time"])
            for fifo_outcome in compute_fifo_outcomes(fills):
                closing_coid = fifo_outcome["closing_client_order_id"]
                entry_coid = fifo_outcome["entry_client_order_id"]
                trade_intent_id = None
                for coid in (closing_coid, entry_coid):
                    if coid is None:
                        continue
                    auth_row = conn.execute(
                        select(authorizations_table.c.trade_intent_id).where(
                            authorizations_table.c.resulting_client_order_id == coid
                        )
                    ).first()
                    if auth_row:
                        trade_intent_id = auth_row[0]
                        break

                stmt = insert(trade_outcomes_table).values(
                    user_id=user_id,
                    trade_intent_id=trade_intent_id,
                    client_order_id=closing_coid,
                    broker_trade_id=fifo_outcome["broker_trade_id"],
                    execution_mode=execution_mode,
                    instrument=instrument,
                    action=fifo_outcome["action"],
                    units=fifo_outcome["units"],
                    entry_price=fifo_outcome["entry_price"],
                    exit_price=fifo_outcome["exit_price"],
                    realized_pl_usd=fifo_outcome["realized_pl_usd"],
                    opened_at=fifo_outcome["opened_at"],
                    closed_at=fifo_outcome["closed_at"],
                    outcome=fifo_outcome["outcome"],
                    synced_at=now,
                    broker="alpaca",
                )
                stmt = stmt.on_conflict_do_nothing(
                    index_elements=["broker", "broker_trade_id", "execution_mode", "closed_at"]
                )
                result = conn.execute(stmt)
                # Real bug found live 2026-10-01: this driver's rowcount is
                # -1 (not 0) on a genuine ON CONFLICT DO NOTHING skip --
                # -1 is truthy, so a naive `if result.rowcount:` claimed
                # every already-synced row as "new" on every single run.
                # Confirmed the DB itself was never affected (on_conflict_
                # do_nothing still correctly skipped the actual insert,
                # verified via a direct row-count check) -- this only fixed
                # a misleading log line, not real duplicate data.
                if result.rowcount > 0:
                    new_count += 1

    return new_count

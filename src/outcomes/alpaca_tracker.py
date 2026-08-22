"""Links closed Alpaca trades back to the trade_intent that caused them —
the Alpaca counterpart to src/outcomes/tracker.py (OANDA), same purpose
(makes "train the model on results" possible: without this, orders_fills
tells you a trade happened and predictions tells you what the model
thought, but nothing connects either to what actually happened to the
money), different mechanics because Alpaca has no OANDA-style transaction
ledger with an explicit tradesClosed[]/originating-tradeID link.

Instead, this walks this system's OWN orders_fills rows (broker='alpaca')
— every entry this system ever placed via ExecutionService, complete with
the exact broker_order_id and client_order_id Alpaca gave it — and asks
Alpaca directly, per entry, "is this filled, and if so, has its exit
happened yet":
  - Equities: entries go out as bracket orders (src/broker/alpaca.py), so
    the exit is one of the parent order's own `legs[]` — whichever leg's
    status is "filled" (Alpaca auto-cancels the other, true OCO).
  - Crypto: entries are plain orders; the protective exit (if any) is a
    SEPARATE order this system placed with client_order_id
    f"{entry_client_order_id}-stop" (see AlpacaBroker._attach_crypto_stop_loss)
    — looked up here by scanning this account's closed orders once per
    sync and matching client_order_id, since Alpaca has no by-client-
    order-id fetch endpoint. Crypto has no take-profit exit in this
    system (see alpaca.py's module docstring on why) — a crypto position
    only ever closes, as far as this tracker can see, via that stop
    filling or a human closing it manually (the latter is untraceable by
    client_order_id and stays "open" from this tracker's perspective,
    same honest-gap posture the OANDA tracker takes for its own
    unresolvable ledger gaps).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.broker.alpaca import AlpacaBroker, parse_alpaca_time
from src.data.db import authorizations as authorizations_table
from src.data.db import orders_fills as orders_fills_table
from src.data.db import trade_outcomes as trade_outcomes_table
from src.data.db import upsert_insert as insert


def _outcome_label(realized_pl: float) -> str:
    if realized_pl > 0:
        return "WIN"
    if realized_pl < 0:
        return "LOSS"
    return "BREAKEVEN"


def _find_exit(entry_order: dict, closed_orders_by_client_id: dict[str, dict]) -> dict | None:
    """The order that closed this entry's position, or None if it's still
    open (from this tracker's point of view — see module docstring on the
    crypto manual-close gap). entry_order must come from AlpacaBroker.
    get_order() (a single-order fetch), not transactions()'s bulk list —
    the bulk list flattens a bracket's legs into separate top-level
    entries and doesn't nest them, so it can't answer this question."""
    legs = entry_order.get("legs")
    if legs:  # equity bracket — the filled leg is the exit, if either has filled yet
        for leg in legs:
            if leg.get("status") == "filled":
                return leg
        return None
    stop_client_id = f"{entry_order['client_order_id']}-stop"
    exit_order = closed_orders_by_client_id.get(stop_client_id)
    if exit_order and exit_order.get("status") == "filled":
        return exit_order
    return None


async def sync_alpaca_outcomes(engine: Engine, broker: AlpacaBroker, user_id: int, execution_mode: str = "demo") -> int:
    """Walks this user's own Alpaca-routed orders_fills entries, checks
    each one against the real Alpaca account for a fill and a resolved
    exit, and upserts into trade_outcomes. Returns the number of newly-
    inserted rows (not updates)."""
    now = datetime.now(timezone.utc)

    with engine.connect() as conn:
        already_tracked = {
            row[0] for row in conn.execute(
                select(trade_outcomes_table.c.client_order_id).where(
                    trade_outcomes_table.c.broker == "alpaca",
                    trade_outcomes_table.c.user_id == user_id,
                )
            )
        }
        entry_fills = conn.execute(
            select(orders_fills_table).where(
                orders_fills_table.c.broker == "alpaca",
                orders_fills_table.c.user_id == user_id,
                # "-stop" rows are never entries themselves — they're not
                # written to orders_fills at all (placed directly by
                # AlpacaBroker, not through ExecutionService), so this
                # filter is defensive, not currently load-bearing.
                ~orders_fills_table.c.client_order_id.like("%-stop"),
            )
        ).mappings().all()

    pending = [dict(f) for f in entry_fills if f["client_order_id"] not in already_tracked]
    if not pending:
        return 0

    # Only used to find a crypto stop-loss order's fill by client_order_id
    # (Alpaca has no fetch-by-client-order-id endpoint) — everything else
    # comes from a per-entry get_order() call below, see its docstring.
    closed_orders = await broker.transactions()
    closed_orders_by_client_id = {o["client_order_id"]: o for o in closed_orders if o.get("client_order_id")}

    new_count = 0
    with engine.begin() as conn:
        for fill in pending:
            coid = fill["client_order_id"]
            if not fill["broker_order_id"]:
                continue  # order placement itself failed — never reached the broker, nothing to sync
            order = await broker.get_order(fill["broker_order_id"])
            if order.get("status") != "filled":
                continue  # not filled yet (or was rejected/cancelled) — try again next sync

            entry_price = float(order["filled_avg_price"]) if order.get("filled_avg_price") else None
            opened_at = parse_alpaca_time(order["filled_at"]) if order.get("filled_at") else None
            side = order.get("side")

            exit_order = _find_exit(order, closed_orders_by_client_id)
            if exit_order is None:
                continue  # position (if any) is still open — try again next sync

            exit_price = float(exit_order["filled_avg_price"])
            # The actual quantity that was bought AND sold — for crypto,
            # Alpaca deducts fees from the position itself (real gap found
            # live 2026-08-21/22, see AlpacaBroker's own docstring), so the
            # exit's filled_qty is the honest "how much really traded"
            # figure, not the entry's nominal requested/filled_qty.
            units = float(exit_order.get("filled_qty") or order.get("filled_qty") or 0)
            closed_at = parse_alpaca_time(exit_order["filled_at"])
            realized_pl = (exit_price - entry_price) * units * (1 if side == "buy" else -1)

            auth_row = conn.execute(
                select(authorizations_table.c.trade_intent_id).where(
                    authorizations_table.c.resulting_client_order_id == coid
                )
            ).first()
            trade_intent_id = auth_row[0] if auth_row else None

            stmt = insert(trade_outcomes_table).values(
                user_id=user_id,
                trade_intent_id=trade_intent_id,
                client_order_id=coid,
                broker_trade_id=fill["broker_order_id"],
                execution_mode=execution_mode,
                instrument=fill["instrument"],
                action="BUY" if side == "buy" else "SELL",
                units=units,
                entry_price=entry_price,
                exit_price=exit_price,
                realized_pl_usd=realized_pl,
                opened_at=opened_at,
                closed_at=closed_at,
                outcome=_outcome_label(realized_pl),
                synced_at=now,
                broker="alpaca",
            )
            stmt = stmt.on_conflict_do_nothing(index_elements=["broker", "broker_trade_id", "execution_mode"])
            result = conn.execute(stmt)
            if result.rowcount:
                new_count += 1

    return new_count

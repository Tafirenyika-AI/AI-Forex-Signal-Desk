"""Links closed broker trades back to the trade_intent that caused them.

This is what makes "train the model on results" possible at all: without
it, orders_fills tells you a trade happened and predictions tells you what
the model thought, but nothing connects either of those to what actually
happened to the money. The link runs:

    OANDA ORDER_FILL transaction with tradesClosed[]
        -> tradesClosed[].tradeID == the id of the ORIGINAL opening
           ORDER_FILL transaction (verified live 2026-08-13 by tracing a
           real stop-loss close back to its entry)
        -> that opening transaction's clientOrderID
        -> authorizations.resulting_client_order_id
        -> authorizations.trade_intent_id
        -> trade_intents / predictions

Earlier versions of this tracker used OANDA's `/trades?state=CLOSED` and
`/trades/{id}` endpoints — verified live to both return nothing for trades
that have already closed on this practice environment (`/trades/{id}`
404s outright). The transaction ledger (`/transactions/idrange`) is the
only source that reliably retains closed-trade history, so that's what
this walks instead.

Only demo (real OANDA account) trades are covered — PaperBroker doesn't
maintain a transaction ledger in v1 (see its docstring), so paper outcomes
aren't tracked here yet.

Note (2026-09-05): `trade_outcomes`' unique constraint now includes
`closed_at`, so a genuine OANDA partial close followed by a later final
close on the same tradeID both get recorded as separate rows instead of
the second one colliding and silently dropping. This means one real trade
can now legitimately produce more than one `trade_outcomes` row — anything
assuming "one row = one round trip" (e.g. the meta-model's linked-outcome
count in train_meta_model.py) should be revisited if partial closes turn
out to be common in practice.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.broker.oanda import OandaBroker, parse_oanda_time
from src.data.db import authorizations as authorizations_table
from src.data.db import trade_outcomes as trade_outcomes_table
from src.data.db import upsert_insert as insert


def _outcome_label(realized_pl: float) -> str:
    if realized_pl > 0:
        return "WIN"
    if realized_pl < 0:
        return "LOSS"
    return "BREAKEVEN"


async def sync_outcomes(engine: Engine, broker: OandaBroker, user_id: int, execution_mode: str = "demo") -> int:
    """Walks the full transaction ledger, finds every ORDER_FILL that closed
    one or more trades, matches each back to its trade_intent (if
    resolvable), and upserts into trade_outcomes. Returns the number of
    newly-inserted rows (not updates)."""
    all_txns = await broker.transactions()
    txns_by_id = {t["id"]: t for t in all_txns if t.get("type") == "ORDER_FILL"}
    now = datetime.now(timezone.utc)
    new_count = 0

    with engine.begin() as conn:
        for txn in all_txns:
            if txn.get("type") != "ORDER_FILL" or not txn.get("tradesClosed"):
                continue

            for closed in txn["tradesClosed"]:
                trade_id = closed["tradeID"]
                opening_txn = txns_by_id.get(trade_id)

                client_order_id = opening_txn.get("clientOrderID") if opening_txn else None
                trade_intent_id = None
                if client_order_id:
                    auth = conn.execute(
                        select(authorizations_table.c.trade_intent_id)
                        .where(authorizations_table.c.resulting_client_order_id == client_order_id)
                    ).first()
                    if auth:
                        trade_intent_id = auth[0]

                opening_units = int(opening_txn["units"]) if opening_txn else -int(closed["units"])
                realized_pl = float(closed["realizedPL"])

                stmt = insert(trade_outcomes_table).values(
                    user_id=user_id,
                    trade_intent_id=trade_intent_id,
                    client_order_id=client_order_id,
                    broker_trade_id=trade_id,
                    execution_mode=execution_mode,
                    instrument=txn["instrument"],
                    action="BUY" if opening_units > 0 else "SELL",
                    units=abs(opening_units),
                    # Honestly None when the opening fill can't be resolved
                    # (real gap found live 2026-08-18: OANDA's transaction
                    # ledger doesn't always retain it — confirmed even a
                    # direct per-ID fetch returns nothing, not just a
                    # narrow-window issue) — never the closing price as a
                    # stand-in, which would silently look like a real entry
                    # price while actually just restating the exit price.
                    entry_price=float(opening_txn["price"]) if opening_txn else None,
                    exit_price=float(txn["price"]),
                    realized_pl_usd=realized_pl,
                    opened_at=parse_oanda_time(opening_txn["time"]) if opening_txn else None,
                    closed_at=parse_oanda_time(txn["time"]),
                    outcome=_outcome_label(realized_pl),
                    synced_at=now,
                    # This tracker only ever walks OANDA's transaction
                    # ledger (see module docstring — Alpaca outcome
                    # tracking is separate, deliberately deferred work),
                    # so this is always correct, not a guess.
                    broker="oanda",
                )
                stmt = stmt.on_conflict_do_nothing(
                    index_elements=["broker", "broker_trade_id", "execution_mode", "closed_at"]
                )
                result = conn.execute(stmt)
                if result.rowcount:
                    new_count += 1

    return new_count

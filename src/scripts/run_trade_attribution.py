"""Computes automated post-trade attribution (Autonomous Upgrade Spec sec.
13) for any trade_outcomes row that doesn't have one yet. Idempotent —
trade_attribution.trade_outcome_id is unique, so already-attributed trades
are skipped on every run.

Candle history for the regime-change check comes from whichever broker
actually trades that outcome's instrument (real market data either way) —
OANDA for forex, Alpaca for equities/crypto once Alpaca outcome tracking
started producing real trade_outcomes rows (src/outcomes/alpaca_tracker.py)
— the trade's own regime state is a property of the market, not of which
broker executed it, but each broker only knows its own instruments.

Run from the project root with the venv active:
    python -m src.scripts.run_trade_attribution
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from src.broker.alpaca import AlpacaBroker
from src.broker.oanda import OandaBroker
from src.broker.registry import broker_kind_for
from src.config import load_settings
from src.data.db import get_engine
from src.data.db import trade_attribution as trade_attribution_table
from src.data.db import trade_outcomes as trade_outcomes_table
from src.data.db import upsert_insert as insert
from src.memory.attribution import attribute_trade


def _naive_to_utc(row: dict) -> dict:
    return {
        k: (v.replace(tzinfo=timezone.utc) if isinstance(v, datetime) and v.tzinfo is None else v)
        for k, v in row.items()
    }


async def main() -> None:
    settings = load_settings()
    engine = get_engine(settings.db_path)
    brokers: dict[str, Any] = {"oanda": OandaBroker(settings)}
    if settings.alpaca_api_key:
        brokers["alpaca"] = AlpacaBroker(settings)

    with engine.connect() as conn:
        attributed_ids = {
            row[0] for row in conn.execute(select(trade_attribution_table.c.trade_outcome_id))
        }
        outcomes = [
            _naive_to_utc(dict(r))
            for r in conn.execute(select(trade_outcomes_table)).mappings().all()
            if r["id"] not in attributed_ids
        ]

    if not outcomes:
        print("No new trade outcomes to attribute.")
        return

    now = datetime.now(timezone.utc)
    computed = 0
    skipped_no_broker = 0
    for outcome in outcomes:
        broker = brokers.get(broker_kind_for(outcome["instrument"]))
        if broker is None:
            skipped_no_broker += 1
            continue  # that broker isn't configured for this user — try again once it is
        result = await attribute_trade(engine, broker, outcome)
        with engine.begin() as conn:
            stmt = insert(trade_attribution_table).values(
                user_id=outcome["user_id"],
                trade_outcome_id=result.trade_outcome_id,
                trade_intent_id=result.trade_intent_id,
                r_multiple=result.r_multiple,
                primary_reason=result.primary_reason,
                contributing_factors_json=json.dumps(result.contributing_factors),
                computed_at=now,
            )
            stmt = stmt.on_conflict_do_nothing(index_elements=["trade_outcome_id"])
            conn.execute(stmt)
        computed += 1
        r_str = f"{result.r_multiple:.2f}" if result.r_multiple is not None else "n/a"
        print(f"trade_outcome {result.trade_outcome_id} ({outcome['instrument']} {outcome['outcome']}): "
              f"R={r_str} primary_reason={result.primary_reason} factors={result.contributing_factors}")

    for broker in brokers.values():
        await broker.close()

    suffix = f" ({skipped_no_broker} skipped — broker not configured)" if skipped_no_broker else ""
    print(f"\nAttributed {computed} trade outcome(s).{suffix}")


if __name__ == "__main__":
    asyncio.run(main())

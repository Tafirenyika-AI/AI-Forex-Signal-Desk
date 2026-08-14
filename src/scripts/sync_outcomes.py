"""Pulls closed trades from the OANDA demo account and links them back to
the trade_intent that caused them, into trade_outcomes.

Run periodically (e.g. every 30-60 minutes is plenty) — trades don't close
faster than that in a 15m-4h-horizon system.

Run from the project root with the venv active:
    python -m src.scripts.sync_outcomes
"""
from __future__ import annotations

import asyncio

from sqlalchemy import func, select

from src.auth.service import active_trading_users
from src.broker.oanda import OandaBroker
from src.config import load_settings
from src.data.db import get_engine
from src.data.db import trade_outcomes as trade_outcomes_table
from src.outcomes.tracker import sync_outcomes


async def main() -> None:
    settings = load_settings()
    engine = get_engine(settings.db_path)

    for user_ctx in active_trading_users(engine):
        if user_ctx.execution_mode != "demo":
            continue  # PaperBroker maintains no transaction ledger (see tracker.py docstring) — nothing to sync
        async with OandaBroker(user_ctx.settings) as broker:
            new_count = await sync_outcomes(engine, broker, user_ctx.user_id, execution_mode="demo")
        print(f"{user_ctx.email}: {new_count} new closed trade(s) recorded this sync")

    with engine.connect() as conn:
        total = conn.execute(select(func.count()).select_from(trade_outcomes_table)).scalar()
        linked = conn.execute(
            select(func.count()).select_from(trade_outcomes_table)
            .where(trade_outcomes_table.c.trade_intent_id.is_not(None))
        ).scalar()
        wins = conn.execute(
            select(func.count()).select_from(trade_outcomes_table).where(trade_outcomes_table.c.outcome == "WIN")
        ).scalar()

    print(f"Total trade_outcomes across all users: {total} ({linked} linked back to a trade_intent, {wins} wins)")


if __name__ == "__main__":
    asyncio.run(main())

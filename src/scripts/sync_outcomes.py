"""Pulls closed trades from Alpaca and links them back to the trade_intent
that caused them, into trade_outcomes.

Run periodically (e.g. every 30-60 minutes is plenty) — trades don't close
faster than that in a 15m-4h-horizon system.

OANDA/forex sync was removed 2026-09-30 (user decision — see
docs/REQUIREMENT_TRACKER.md): this used to unconditionally re-walk OANDA's
full transaction ledger and re-insert its real closed trades every run.
Once forex data was deleted from trade_outcomes, that loop would silently
resurrect it on the very next sync — the ledger lives at the broker, not
in our DB, so a local delete doesn't make the broker forget its own
history. src/outcomes/tracker.py's sync_outcomes() (OANDA-specific) is no
longer called from anywhere live.

Run from the project root with the venv active:
    python -m src.scripts.sync_outcomes
"""
from __future__ import annotations

import asyncio

from sqlalchemy import func, select

from src.auth.service import active_trading_users
from src.broker.alpaca import AlpacaBroker
from src.config import load_settings
from src.data.db import get_engine
from src.data.db import trade_outcomes as trade_outcomes_table
from src.outcomes.alpaca_tracker import sync_alpaca_outcomes


async def main() -> None:
    settings = load_settings()
    engine = get_engine(settings.db_path)

    for user_ctx in active_trading_users(engine):
        # Alpaca's own paper account is always the real (non-simulated) one
        # — see src/broker/alpaca.py's module docstring.
        if user_ctx.settings.alpaca_api_key:
            async with AlpacaBroker(user_ctx.settings) as broker:
                new_count = await sync_alpaca_outcomes(engine, broker, user_ctx.user_id, execution_mode="demo")
            print(f"{user_ctx.email}: {new_count} new closed Alpaca trade(s) recorded this sync")

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

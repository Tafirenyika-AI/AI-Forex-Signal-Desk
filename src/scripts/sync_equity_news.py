"""Equity V2 Phase 6 scheduled entrypoint — two jobs, run back to back:

1. Fetch real Alpaca News articles for every equity ticker an active user
   trades, insert new ones into equity_news (on_conflict_do_nothing on
   url — idempotent re-run, same pattern Phase 4's SEC sync already uses).
2. Compute real price_reaction_1h/1d for any equity_news row old enough
   for both windows to have genuine candle data, using this project's own
   already-stored candles — never estimated at publish time.

Read-only against the broker/market-data API (Alpaca's News endpoint is a
GET, and reaction computation only reads from the candles table this
project already maintains) — no broker call anywhere capable of touching
an order or position.

News volume changes faster than SEC filings but still not every few
minutes — every 30-60 minutes is reasonable, paired with the daily SEC
sync's cadence philosophy (Phase 4).

Run from the project root with the venv active:
    python -m src.scripts.sync_equity_news
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import func, select

from src.auth.service import active_trading_users
from src.broker.registry import asset_class_for
from src.config import load_settings
from src.data.db import equity_news as equity_news_table
from src.data.db import get_engine
from src.data.db import upsert_insert as insert
from src.news.equity_news import AlpacaNewsClient, compute_reaction, pending_reaction_rows


def _equity_tickers(engine) -> set[str]:
    tickers: set[str] = set()
    for user_ctx in active_trading_users(engine):
        for instrument in user_ctx.instrument_list:
            if asset_class_for(instrument) == "equity":
                tickers.add(instrument.upper())
    return tickers


async def _sync_news(engine, settings) -> int:
    tickers = sorted(_equity_tickers(engine))
    if not tickers:
        print("No equity tickers configured by any active user — skipping news fetch.")
        return 0
    if not settings.alpaca_api_key or not settings.alpaca_api_secret:
        print("No Alpaca credentials configured — skipping news fetch.")
        return 0

    async with AlpacaNewsClient(settings.alpaca_api_key, settings.alpaca_api_secret) as client:
        rows = await client.news_rows_for(tickers, limit=50)

    if not rows:
        print(f"No new articles returned for {len(tickers)} ticker(s).")
        return 0

    with engine.begin() as conn:
        # rowcount from a bulk executemany insert is not reliable across
        # drivers (the same psycopg executemany limitation already known
        # in this project, see src/outcomes/alpaca_tracker.py's own
        # rowcount==-1 fix) -- counting before/after in the same
        # transaction is the only way to honestly report how many of these
        # articles were genuinely new, confirmed live 2026-10-02 (the
        # naive rowcount read reported 0 new while 50 rows had actually
        # just been inserted).
        before = conn.execute(select(func.count()).select_from(equity_news_table)).scalar()
        stmt = insert(equity_news_table).on_conflict_do_nothing(index_elements=["url"])
        conn.execute(stmt, rows)
        after = conn.execute(select(func.count()).select_from(equity_news_table)).scalar()
    print(f"Fetched {len(rows)} article(s) for {len(tickers)} ticker(s) ({tickers}); "
          f"{after - before} newly inserted.")
    return len(rows)


def _compute_reactions(engine) -> int:
    now = datetime.now(timezone.utc)
    pending = pending_reaction_rows(engine, now)
    if not pending:
        print("No equity_news rows are due for reaction computation.")
        return 0

    updated = 0
    with engine.begin() as conn:
        for row in pending:
            tickers = row["tickers"].split(",")
            # Multi-ticker articles: compute against the first listed
            # ticker — a genuinely multi-company article (e.g. the
            # "most-searched tickers" roundup seen live in verification)
            # doesn't have one unambiguous "the" reaction, and this project
            # has no per-article primary-ticker signal to prefer one over
            # another; disclosed as a known limitation, not silently exact.
            reaction_1h, reaction_1d = compute_reaction(engine, tickers[0], row["publish_time"])
            conn.execute(
                equity_news_table.update()
                .where(equity_news_table.c.id == row["id"])
                .values(price_reaction_1h=reaction_1h, price_reaction_1d=reaction_1d, reaction_computed_at=now)
            )
            updated += 1
    print(f"Computed reactions for {updated} article(s).")
    return updated


async def main() -> None:
    settings = load_settings()
    engine = get_engine(settings.db_path)

    await _sync_news(engine, settings)
    _compute_reactions(engine)


if __name__ == "__main__":
    asyncio.run(main())

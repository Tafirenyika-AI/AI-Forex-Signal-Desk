"""Equity V2 Phase 4 scheduled entrypoint — pulls real SEC EDGAR structured
financials for every equity ticker any active user actually trades, and
upserts them into equity_entities/company_fundamentals.

Read-only against SEC's public API (no broker call anywhere in this
script — src/equity/sec_edgar.py imports nothing from src.execution or
src.broker), and every write is additive: equity_entities is a current-
state upsert (sector/CIK can only get MORE accurate over time, never
destructively overwritten with worse data), company_fundamentals rows are
inserted with on_conflict_do_nothing — a re-run is a safe no-op for
anything already stored, and a genuine SEC restatement (a later filing
revising an earlier period) lands as an additional row, not an overwrite
(see that table's own point-in-time design in src/data/db.py).

Filings don't change every few minutes — companies file quarterly/annually
— so this is meant to run far less often than the trading cycle or the
reconciliation check (daily is plenty; see the scheduled task this is
paired with).

Run from the project root with the venv active:
    python -m src.scripts.sync_sec_edgar
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from src.auth.service import active_trading_users
from src.broker.registry import asset_class_for
from src.config import load_settings
from src.data.db import company_fundamentals as company_fundamentals_table
from src.data.db import equity_entities as equity_entities_table
from src.data.db import get_engine
from src.data.db import upsert_insert as insert
from src.equity.sec_edgar import SecEdgarClient

# Real contact identifying this project to SEC's fair-access policy, per
# the user's explicit choice (2026-10-02) to use a real, reachable address
# rather than a placeholder.
SEC_EDGAR_CONTACT_EMAIL = "tafishoniwa@gmail.com"


def _equity_tickers(engine) -> set[str]:
    tickers: set[str] = set()
    for user_ctx in active_trading_users(engine):
        for instrument in user_ctx.instrument_list:
            if asset_class_for(instrument) == "equity":
                tickers.add(instrument.upper())
    return tickers


async def _sync_one(client: SecEdgarClient, engine, ticker: str) -> tuple[int, int]:
    """Returns (fundamentals_rows_written, 1 if entity upserted else 0)."""
    lookup = await client.lookup(ticker)
    if lookup is None:
        print(f"{ticker}: no SEC CIK found (not a SEC-registered filer, or a typo) — skipping")
        return 0, 0

    cik, title = lookup["cik"], lookup["title"]
    entity_row = client.entity_row(ticker, cik, title)
    rows = await client.fundamentals_rows(ticker, cik)

    with engine.begin() as conn:
        entity_stmt = insert(equity_entities_table)
        entity_stmt = entity_stmt.on_conflict_do_update(
            index_elements=["ticker"],
            set_={
                "cik": entity_stmt.excluded.cik,
                "company_name": entity_stmt.excluded.company_name,
                "updated_at": entity_stmt.excluded.updated_at,
            },
        )
        conn.execute(entity_stmt, entity_row)

        if rows:
            fundamentals_stmt = insert(company_fundamentals_table)
            fundamentals_stmt = fundamentals_stmt.on_conflict_do_nothing(
                index_elements=["ticker", "period_start", "period_end", "metric", "source", "filed_at"],
            )
            conn.execute(fundamentals_stmt, rows)

    return len(rows), 1


async def main() -> None:
    settings = load_settings()
    engine = get_engine(settings.db_path)

    tickers = sorted(_equity_tickers(engine))
    if not tickers:
        print("No equity tickers configured by any active user — nothing to sync.")
        return

    print(f"Syncing SEC EDGAR fundamentals for {len(tickers)} equity ticker(s): {', '.join(tickers)}")
    async with SecEdgarClient(contact_email=SEC_EDGAR_CONTACT_EMAIL) as client:
        for ticker in tickers:
            try:
                row_count, entity_count = await _sync_one(client, engine, ticker)
                print(f"  {ticker}: {row_count} fundamentals row(s) considered, "
                      f"entity {'upserted' if entity_count else 'skipped'}")
            except Exception as exc:  # noqa: BLE001 — one ticker's failure shouldn't stop the rest
                print(f"  {ticker}: FAILED — {exc!r}")

    print(f"Done at {datetime.now(timezone.utc).isoformat()}")


if __name__ == "__main__":
    asyncio.run(main())

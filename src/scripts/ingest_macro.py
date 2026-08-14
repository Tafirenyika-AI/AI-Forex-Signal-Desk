"""Phase 0/Sprint 2: pull FRED macro data for all v1 currencies into economic_events.

Run from the project root with the venv active:
    python -m src.scripts.ingest_macro

No-ops cleanly (prints a notice) if FRED_API_KEY is not set in .env.
Get a free key: https://fred.stlouisfed.org/docs/api/api_key.html
"""
from __future__ import annotations

import asyncio

from src.config import load_settings
from src.data.db import economic_events
from src.data.db import get_engine
from src.data.db import upsert_insert as insert
from src.macro.fred import FredClient

CURRENCIES = ["USD", "EUR", "GBP", "JPY", "CAD", "AUD"]


async def main() -> None:
    settings = load_settings()
    engine = get_engine(settings.db_path)

    async with FredClient(settings.fred_api_key) as client:
        if not client.configured:
            print(
                "FRED_API_KEY not set in .env — skipping macro ingestion.\n"
                "Get a free key at https://fred.stlouisfed.org/docs/api/api_key.html "
                "and set FRED_API_KEY in .env, then re-run this script."
            )
            return

        total = 0
        with engine.begin() as conn:
            for currency in CURRENCIES:
                rows = await client.latest_event_rows(currency)
                if not rows:
                    continue
                stmt = insert(economic_events)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["event_time", "currency", "event_name", "source"],
                    set_={
                        "actual": stmt.excluded.actual,
                        "previous": stmt.excluded.previous,
                        "ingested_at": stmt.excluded.ingested_at,
                    },
                )
                conn.execute(stmt, rows)
                total += len(rows)
                for row in rows:
                    print(f"  {currency} {row['event_name']}: "
                          f"actual={row['actual']} previous={row['previous']} "
                          f"(as of {row['event_time'].date()})")
        print(f"Stored/updated {total} economic_events rows.")


if __name__ == "__main__":
    asyncio.run(main())

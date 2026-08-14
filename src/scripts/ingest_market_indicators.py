"""Cross-market confirmation data (blueprint sec. 6, row 5): Treasury yields,
a broad-dollar-index proxy, and WTI crude — all free via FRED.

Run from the project root with the venv active:
    python -m src.scripts.ingest_market_indicators

No-ops cleanly (prints a notice) if FRED_API_KEY is not set in .env.
"""
from __future__ import annotations

import asyncio

from src.config import load_settings
from src.data.db import get_engine
from src.data.db import market_indicators as market_indicators_table
from src.data.db import upsert_insert as insert
from src.macro.fred import FredClient


async def main() -> None:
    settings = load_settings()
    engine = get_engine(settings.db_path)

    async with FredClient(settings.fred_api_key) as client:
        if not client.configured:
            print(
                "FRED_API_KEY not set in .env — skipping market indicator ingestion.\n"
                "Get a free key at https://fred.stlouisfed.org/docs/api/api_key.html"
            )
            return

        rows = await client.market_indicator_rows(limit=60)
        if not rows:
            print("No market indicator rows returned.")
            return

        with engine.begin() as conn:
            stmt = insert(market_indicators_table)
            stmt = stmt.on_conflict_do_update(
                index_elements=["indicator", "observation_date", "source"],
                set_={"value": stmt.excluded.value, "ingested_at": stmt.excluded.ingested_at},
            )
            conn.execute(stmt, rows)

        print(f"Stored/updated {len(rows)} market_indicators rows.")
        latest_by_indicator: dict[str, dict] = {}
        for row in rows:
            latest_by_indicator[row["indicator"]] = row
        for indicator, row in latest_by_indicator.items():
            print(f"  {indicator}: {row['value']} (as of {row['observation_date'].date()})")


if __name__ == "__main__":
    asyncio.run(main())

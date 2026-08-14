"""Sprint 3: pull Alpha Vantage forex-tagged news into news_events.

Run from the project root with the venv active:
    python -m src.scripts.ingest_news

No-ops cleanly (prints a notice) if ALPHAVANTAGE_API_KEY is not set in .env.
Get a free key: https://www.alphavantage.co/support/#api-key
"""
from __future__ import annotations

import asyncio

from sqlalchemy.dialects.sqlite import insert

from src.config import load_settings
from src.data.db import get_engine
from src.data.db import news_events as news_events_table
from src.news.alpha_vantage import AlphaVantageNewsClient


async def main() -> None:
    settings = load_settings()
    engine = get_engine(settings.db_path)

    async with AlphaVantageNewsClient(settings.alphavantage_api_key) as client:
        if not client.configured:
            print(
                "ALPHAVANTAGE_API_KEY not set in .env — skipping news ingestion.\n"
                "Get a free key at https://www.alphavantage.co/support/#api-key "
                "and set ALPHAVANTAGE_API_KEY in .env, then re-run this script."
            )
            return

        rows = await client.fetch_forex_news(limit=50)
        if not rows:
            print("No forex-tagged news returned.")
            return

        with engine.begin() as conn:
            for row in rows:
                stmt = insert(news_events_table).values(**row)
                stmt = stmt.on_conflict_do_nothing(index_elements=["url"])
                conn.execute(stmt)

        print(f"Stored {len(rows)} news_events rows (duplicates by URL skipped).")
        for row in rows[:5]:
            print(f"  [{row['currencies']}] {row['headline'][:80]} "
                  f"(sentiment={row['sentiment_score']})")


if __name__ == "__main__":
    asyncio.run(main())

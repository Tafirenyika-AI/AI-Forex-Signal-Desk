"""Phase 0 data ingestion: pull historical candles for the v1 pairs and store them.

Run from the project root with the venv active:
    python -m src.scripts.download_candles
"""
from __future__ import annotations

import asyncio

from sqlalchemy.dialects.sqlite import insert

from src.broker.oanda import OandaBroker
from src.config import load_settings
from src.data.db import candles as candles_table
from src.data.db import get_engine

PAIRS = ["EUR_USD", "GBP_USD", "USD_JPY", "USD_CAD", "AUD_USD"]
GRANULARITIES = ["M15", "H1", "H4"]


async def main() -> None:
    settings = load_settings()
    engine = get_engine(settings.db_path)

    async with OandaBroker(settings) as broker:
        with engine.begin() as conn:
            for pair in PAIRS:
                for granularity in GRANULARITIES:
                    candles = await broker.get_candles(pair, granularity, count=500)
                    if not candles:
                        continue
                    rows = [
                        {
                            "instrument": c.instrument,
                            "granularity": c.granularity,
                            "time": c.time,
                            "open": c.open,
                            "high": c.high,
                            "low": c.low,
                            "close": c.close,
                            "volume": c.volume,
                            "complete": c.complete,
                        }
                        for c in candles
                    ]
                    stmt = insert(candles_table)
                    stmt = stmt.on_conflict_do_update(
                        index_elements=["instrument", "granularity", "time"],
                        set_={
                            "open": stmt.excluded.open,
                            "high": stmt.excluded.high,
                            "low": stmt.excluded.low,
                            "close": stmt.excluded.close,
                            "volume": stmt.excluded.volume,
                            "complete": stmt.excluded.complete,
                        },
                    )
                    conn.execute(stmt, rows)
                    print(f"{pair} {granularity}: stored {len(rows)} candles "
                          f"({candles[0].time} .. {candles[-1].time})")


if __name__ == "__main__":
    asyncio.run(main())

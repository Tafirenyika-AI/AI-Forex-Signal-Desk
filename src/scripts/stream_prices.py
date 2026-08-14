"""Phase 0 data ingestion: continuously stream live prices and persist them.

Run from the project root with the venv active:
    python -m src.scripts.stream_prices

Stop with Ctrl+C. Reconnects automatically on stream drops (blueprint sec. 14
Phase 0 requires verifying reconnects and database integrity).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import insert

from src.broker.oanda import OandaBroker
from src.config import load_settings
from src.data.db import get_engine
from src.data.db import market_prices as market_prices_table

PAIRS = ["EUR_USD", "GBP_USD", "USD_JPY", "USD_CAD", "AUD_USD"]
RECONNECT_DELAY_SECONDS = 5


async def run_stream(broker: OandaBroker, engine) -> None:
    count = 0
    async for price in broker.stream_prices(PAIRS):
        with engine.begin() as conn:
            conn.execute(
                insert(market_prices_table),
                {
                    "instrument": price.instrument,
                    "time": price.time,
                    "bid": price.bid,
                    "ask": price.ask,
                    "ingested_at": datetime.now(timezone.utc),
                },
            )
        count += 1
        if count % 20 == 0:
            print(f"[{datetime.now(timezone.utc).isoformat()}] "
                  f"{count} prices stored (last: {price.instrument} "
                  f"bid={price.bid} ask={price.ask})")


async def main() -> None:
    settings = load_settings()
    engine = get_engine(settings.db_path)

    async with OandaBroker(settings) as broker:
        while True:
            try:
                print(f"Connecting to price stream for: {', '.join(PAIRS)}")
                await run_stream(broker, engine)
            except Exception as exc:  # noqa: BLE001 - top-level reconnect loop
                print(f"Stream error: {exc!r}. Reconnecting in "
                      f"{RECONNECT_DELAY_SECONDS}s...")
                await asyncio.sleep(RECONNECT_DELAY_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())

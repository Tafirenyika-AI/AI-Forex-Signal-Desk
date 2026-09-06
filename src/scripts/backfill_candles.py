"""Real historical candle backfill — src/scripts/download_candles.py does a
single 500-bar snapshot per pair with no accumulation (re-running it just
re-fetches the same trailing window); this script instead builds real
multi-month/multi-year depth per (broker, instrument, granularity), and is
resumable: it only fetches what isn't already stored, on every run.

OANDA's v3 candles endpoint caps at 5000 bars per request (per their own
docs), so a deep backfill has to be chunked in time windows sized to stay
under that per granularity, not fetched in one call.

Two gaps this fills relative to what's already stored: (1) forward —
bring existing history up to "now" if it's gone stale, (2) backward —
extend existing history further into the past, up to TARGET_LOOKBACK for
that granularity, working backward one chunk at a time from whatever's
already the oldest stored bar.

Run from the project root with the venv active:
    python -m src.scripts.backfill_candles
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from src.broker.oanda import OandaBroker
from src.config import load_settings
from src.data.db import candles as candles_table
from src.data.db import get_engine
from src.data.db import upsert_insert as insert

# Deep for H1/H4 (the granularities the price model and backtest engine
# actually target), more modest for M15 — it explodes fastest in row count
# (~35k bars/year/pair) for the horizon that matters least (1-bar-ahead).
TARGET_LOOKBACK = {
    "M15": timedelta(days=60),
    "H1": timedelta(days=365),
    "H4": timedelta(days=730),
}
# OANDA's 5000-bar-per-request cap, converted to a time window per
# granularity with headroom (not run right up against the exact limit).
CHUNK_WINDOW = {
    "M15": timedelta(days=45),
    "H1": timedelta(days=180),
    "H4": timedelta(days=700),
}
GRANULARITIES = ["M15", "H1", "H4"]


async def _fetch_and_store(
    broker: OandaBroker, engine, instrument: str, granularity: str,
    from_time: datetime, to_time: datetime,
) -> int:
    candles = await broker.get_candles_range(instrument, granularity, from_time, to_time)
    if not candles:
        return 0
    rows = [
        {
            "instrument": c.instrument, "granularity": c.granularity, "time": c.time,
            "open": c.open, "high": c.high, "low": c.low, "close": c.close,
            "volume": c.volume, "complete": c.complete, "broker": "oanda",
        }
        for c in candles
    ]
    with engine.begin() as conn:
        stmt = insert(candles_table)
        stmt = stmt.on_conflict_do_update(
            index_elements=["broker", "instrument", "granularity", "time"],
            set_={
                "open": stmt.excluded.open, "high": stmt.excluded.high, "low": stmt.excluded.low,
                "close": stmt.excluded.close, "volume": stmt.excluded.volume, "complete": stmt.excluded.complete,
            },
        )
        conn.execute(stmt, rows)
    return len(rows)


async def _backfill_one(broker: OandaBroker, engine, instrument: str, granularity: str) -> None:
    # Real bug found live: OANDA rejects a 'to' timestamp of exactly "now"
    # ("Time is in the future") but accepts one with even a small buffer
    # subtracted — a minute of slack avoids this without meaningfully
    # affecting how current the backfill is.
    now = datetime.now(timezone.utc) - timedelta(minutes=1)
    target_start = now - TARGET_LOOKBACK[granularity]
    chunk = CHUNK_WINDOW[granularity]

    with engine.connect() as conn:
        existing_min, existing_max = conn.execute(
            select(func.min(candles_table.c.time), func.max(candles_table.c.time)).where(
                candles_table.c.broker == "oanda",
                candles_table.c.instrument == instrument,
                candles_table.c.granularity == granularity,
            )
        ).first()

    total = 0

    # Forward gap: bring existing history up to now.
    if existing_max is not None and existing_max < now:
        total += await _fetch_and_store(broker, engine, instrument, granularity, existing_max, now)

    # Backward gap: extend existing history further into the past, one
    # chunk at a time, until reaching TARGET_LOOKBACK.
    cursor_end = existing_min if existing_min is not None else now
    while cursor_end > target_start:
        cursor_start = max(target_start, cursor_end - chunk)
        total += await _fetch_and_store(broker, engine, instrument, granularity, cursor_start, cursor_end)
        cursor_end = cursor_start

    print(f"{instrument} {granularity}: {total} bar(s) stored/updated "
          f"(target back to {target_start.date()})")


async def main() -> None:
    settings = load_settings()
    engine = get_engine(settings.db_path)

    async with OandaBroker(settings) as broker:
        instruments = await broker.list_instruments()
        pairs = sorted(instruments.keys())
        print(f"Backfilling {len(pairs)} OANDA instrument(s) x {len(GRANULARITIES)} granularities...")
        for pair in pairs:
            for granularity in GRANULARITIES:
                try:
                    await _backfill_one(broker, engine, pair, granularity)
                except Exception as exc:  # noqa: BLE001 — one pair's failure shouldn't stop the rest
                    print(f"{pair} {granularity}: FAILED — {exc!r}")


if __name__ == "__main__":
    asyncio.run(main())

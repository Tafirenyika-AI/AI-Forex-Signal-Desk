"""Real historical candle backfill — src/scripts/download_candles.py does a
single 500-bar snapshot per pair with no accumulation (re-running it just
re-fetches the same trailing window); this script instead builds real
multi-month/multi-year depth per (broker, instrument, granularity), and is
resumable: it only fetches what isn't already stored, on every run.

OANDA/forex backfill was removed 2026-10-02 (standing user decision, see
docs/EQUITY_V2_AUDIT.md's "Standing scope note" — OANDA/forex was
permanently, deliberately erased on 2026-09-30, not paused): this used to
unconditionally re-walk OANDA's full instrument list on every run, which
would silently resurrect real OANDA candle rows into a table the user
explicitly asked to have forex data gone from. Alpaca-only from here on,
matching every other sync script already updated to this standard (see
src/scripts/sync_outcomes.py's own removal note).

Two gaps this fills relative to what's already stored: (1) forward —
bring existing history up to "now" if it's gone stale, (2) backward —
extend existing history further into the past, up to TARGET_LOOKBACK for
that granularity, working backward one chunk at a time from whatever's
already the oldest stored bar.

Alpaca's instrument universe (AlpacaBroker.list_instruments()) is every
tradable US equity + crypto pair on their platform — thousands, almost
all irrelevant here. This script instead backfills only BENCHMARK_
INSTRUMENTS (SPY + every sector ETF Phase 7 can resolve to — reference
infrastructure for cross-market features, Phase 8) plus whatever's
actually configured on some real user's instrument list
(active_trading_users), same instrument-routing logic run_loop.py uses.

Run from the project root with the venv active:
    python -m src.scripts.backfill_candles
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from src.auth.service import active_trading_users
from src.broker.alpaca import AlpacaBroker
from src.broker.registry import BrokerKind, broker_kind_for
from src.config import load_settings
from src.data.db import candles as candles_table
from src.data.db import get_engine
from src.data.db import upsert_insert as insert
from src.equity.relationships import SIC_TO_SECTOR

# Equity V2 Phase 8: cross-market features need SPY (the broad-market
# benchmark) and every sector ETF Phase 7's SIC_TO_SECTOR can ever resolve
# to, regardless of whether any user actually trades them — they're
# reference infrastructure for relative-strength/benchmark features on
# whatever equities users DO trade, not trading choices themselves.
# Derived from SIC_TO_SECTOR (not a separate hardcoded list) so a new
# sector ETF added there is automatically backfilled too.
#
# V4 Priority 2 (docs/V4_ARCHITECTURE.md Section 4 gap): IWM (small-cap
# benchmark, distinct regime from SPY's large-cap-heavy composition),
# GLD/IAU (gold - the standard risk-off/inflation-hedge cross-market
# reference), USO (oil - a real macro driver for energy-sector names
# already covered via XLE but not as a standalone cross-market signal).
# Same reference-infrastructure rationale as SPY/the sector ETFs above:
# backfilled regardless of whether any user trades them.
BENCHMARK_INSTRUMENTS = {"SPY", "IWM", "GLD", "IAU", "USO"} | {etf for _, (_, etf) in SIC_TO_SECTOR}

# Deep for H1/H4 (the granularities the price model and backtest engine
# actually target), more modest for M15 — it explodes fastest in row count
# (~35k bars/year/pair) for the horizon that matters least (1-bar-ahead).
TARGET_LOOKBACK = {
    "M15": timedelta(days=60),
    "H1": timedelta(days=365),
    "H4": timedelta(days=730),
}
# Chunk size per granularity -- originally sized to OANDA's 5000-bar-per-
# request cap with headroom; kept as-is for Alpaca (no such cap applies,
# see module docstring) purely so one set of windows still works for any
# future broker this script might backfill again.
CHUNK_WINDOW = {
    "M15": timedelta(days=45),
    "H1": timedelta(days=180),
    "H4": timedelta(days=700),
}
GRANULARITIES = ["M15", "H1", "H4"]


async def _fetch_and_store(
    broker, engine, broker_kind: BrokerKind, instrument: str, granularity: str,
    from_time: datetime, to_time: datetime,
) -> int:
    candles = await broker.get_candles_range(instrument, granularity, from_time, to_time)
    if not candles:
        return 0
    rows = [
        {
            "instrument": c.instrument, "granularity": c.granularity, "time": c.time,
            "open": c.open, "high": c.high, "low": c.low, "close": c.close,
            "volume": c.volume, "complete": c.complete, "broker": broker_kind,
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


async def _backfill_one(broker, engine, broker_kind: BrokerKind, instrument: str, granularity: str) -> None:
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
                candles_table.c.broker == broker_kind,
                candles_table.c.instrument == instrument,
                candles_table.c.granularity == granularity,
            )
        ).first()

    total = 0

    # Forward gap: bring existing history up to now.
    if existing_max is not None and existing_max < now:
        total += await _fetch_and_store(broker, engine, broker_kind, instrument, granularity, existing_max, now)

    # Backward gap: extend existing history further into the past, one
    # chunk at a time, until reaching TARGET_LOOKBACK.
    cursor_end = existing_min if existing_min is not None else now
    while cursor_end > target_start:
        cursor_start = max(target_start, cursor_end - chunk)
        total += await _fetch_and_store(broker, engine, broker_kind, instrument, granularity, cursor_start, cursor_end)
        cursor_end = cursor_start

    print(f"{instrument} {granularity}: {total} bar(s) stored/updated "
          f"(target back to {target_start.date()})")


async def main() -> None:
    settings = load_settings()
    engine = get_engine(settings.db_path)

    if not settings.alpaca_api_key:
        print("No Alpaca credentials configured — skipping Alpaca backfill.")
        return

    alpaca_instruments: set[str] = set(BENCHMARK_INSTRUMENTS)
    for user_ctx in active_trading_users(engine):
        for instrument in user_ctx.instrument_list:
            if broker_kind_for(instrument) == "alpaca":
                alpaca_instruments.add(instrument)
    alpaca_pairs = sorted(alpaca_instruments)

    async with AlpacaBroker(settings) as broker:
        print(f"Backfilling {len(alpaca_pairs)} Alpaca instrument(s) x {len(GRANULARITIES)} granularities...")
        for pair in alpaca_pairs:
            for granularity in GRANULARITIES:
                try:
                    await _backfill_one(broker, engine, "alpaca", pair, granularity)
                except Exception as exc:  # noqa: BLE001 — one pair's failure shouldn't stop the rest
                    print(f"{pair} {granularity}: FAILED — {exc!r}")


if __name__ == "__main__":
    asyncio.run(main())

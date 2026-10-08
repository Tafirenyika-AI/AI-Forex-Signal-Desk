"""One-off migration — V4 Priority 6 (corporate-action awareness): back-
adjusts 5 real Select Sector SPDR ETFs (XLB, XLE, XLK, XLU, XLY) for their
real, confirmed 2-for-1 share split effective 2025-12-04/05.

Found live 2026-10-08 by src/data/corporate_actions.py's new split
detector, run against every currently-backfilled instrument — it flagged
exactly these 5 tickers on exactly this date and no others. Confirmed as
a REAL corporate action (not a data artifact) via a live web search: State
Street executed a real 2-for-1 split of these exact 5 Select Sector SPDR
ETFs on 2025-12-04, trading post-split from 2025-12-05 — see
docs/V4_IMPLEMENTATION_LOG.md's Priority 6 entry for the citation.

Root cause: src/broker/alpaca.py never passes Alpaca's `adjustment`
parameter on bar requests, so all backfilled history is RAW/unadjusted —
each individual historical price is the real trading price at that time,
but comparing pre- and post-split prices directly (returns, moving
averages, regime features) sees a fake ~50% "crash" that never
represented any real economic loss to a holder.

Idempotent: before adjusting a ticker, compares its last pre-cutoff close
against its first post-cutoff close — already-adjusted data has a ratio
near 1.0 (not ~2.0) and is skipped, safe to re-run.

Run from the project root with the venv active:
    python -m src.scripts.fix_spdr_2025_split
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.engine import Engine

from src.config import load_settings
from src.data.db import candles as candles_table
from src.data.db import get_engine

SPLIT_CUTOFF = datetime(2025, 12, 5, 0, 0, tzinfo=timezone.utc)
AFFECTED_TICKERS = ("XLB", "XLE", "XLK", "XLU", "XLY")


def _already_adjusted(engine: Engine, ticker: str) -> bool | None:
    """None if there isn't enough real data on both sides of the cutoff to
    judge either way (e.g., a granularity with no bars spanning the
    split) — never guessed."""
    with engine.connect() as conn:
        last_pre = conn.execute(
            select(candles_table.c.close).where(
                candles_table.c.broker == "alpaca", candles_table.c.instrument == ticker,
                candles_table.c.time < SPLIT_CUTOFF,
            ).order_by(candles_table.c.time.desc()).limit(1)
        ).scalar()
        first_post = conn.execute(
            select(candles_table.c.close).where(
                candles_table.c.broker == "alpaca", candles_table.c.instrument == ticker,
                candles_table.c.time >= SPLIT_CUTOFF,
            ).order_by(candles_table.c.time.asc()).limit(1)
        ).scalar()
    if last_pre is None or first_post is None or first_post == 0:
        return None
    ratio = last_pre / first_post
    return ratio < 1.3  # already-adjusted data: ratio ~1.0; raw/unadjusted: ratio ~2.0


def run() -> None:
    settings = load_settings()
    engine = get_engine(settings.db_path)

    for ticker in AFFECTED_TICKERS:
        already = _already_adjusted(engine, ticker)
        if already is None:
            print(f"{ticker}: no real data spanning the split boundary for any granularity, skipping")
            continue
        if already:
            print(f"{ticker}: already adjusted, skipping")
            continue

        with engine.begin() as conn:
            result = conn.execute(
                update(candles_table).where(
                    candles_table.c.broker == "alpaca", candles_table.c.instrument == ticker,
                    candles_table.c.time < SPLIT_CUTOFF,
                ).values(
                    open=candles_table.c.open * 0.5, high=candles_table.c.high * 0.5,
                    low=candles_table.c.low * 0.5, close=candles_table.c.close * 0.5,
                    volume=candles_table.c.volume * 2,
                )
            )
            print(f"{ticker}: back-adjusted {result.rowcount} pre-split rows (all granularities)")


if __name__ == "__main__":
    run()

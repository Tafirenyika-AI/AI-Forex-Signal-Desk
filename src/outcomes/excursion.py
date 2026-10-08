"""AI Trading Desk V4 Priority 2 (docs/V4_ARCHITECTURE.md Section 14 gap):
max favorable / adverse excursion (MFE/MAE) for closed trades.

MFE is the best unrealized USD gain the trade ever reached between
opened_at and closed_at; MAE is the worst unrealized USD loss. Both are
computed purely from real stored candle highs/lows (src/data/db.py's
`candles` table) — no new data source, no broker call, research-only (never
fed back into any live risk/sizing decision in this pass).

Sign convention matches trade_outcomes.realized_pl_usd: mfe_usd >= 0,
mae_usd <= 0, both direction-adjusted so "BUY" (the position was opened
long) and "SELL" (opened short) are handled symmetrically.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.data.db import candles as candles_table
from src.data.db import trade_outcomes as trade_outcomes_table

# Preference order: finest granularity first, same ones backfill_candles.py
# maintains (src/scripts/backfill_candles.py's TARGET_LOOKBACK). A trade
# older than M15's 60-day retention window falls through to H1/H4.
_GRANULARITY_PREFERENCE = ("M15", "H1", "H4")


def _candle_extremes(
    engine: Engine, broker: str, instrument: str, opened_at: datetime, closed_at: datetime,
) -> tuple[float, float] | None:
    """Returns (highest_high, lowest_low) across whichever granularity has
    real stored candles for this exact window, trying finest-first. None if
    no granularity has any — never fabricated, honestly absent."""
    with engine.connect() as conn:
        for granularity in _GRANULARITY_PREFERENCE:
            rows = conn.execute(
                select(candles_table.c.high, candles_table.c.low).where(
                    candles_table.c.broker == broker,
                    candles_table.c.instrument == instrument,
                    candles_table.c.granularity == granularity,
                    candles_table.c.time >= opened_at,
                    candles_table.c.time <= closed_at,
                )
            ).all()
            if rows:
                return max(r.high for r in rows), min(r.low for r in rows)
    return None


def compute_mfe_mae(
    engine: Engine, broker: str, instrument: str, action: str, units: float,
    entry_price: float | None, exit_price: float, opened_at: datetime | None, closed_at: datetime,
) -> tuple[float | None, float | None]:
    """Pure computation, no DB write. Returns (mfe_usd, mae_usd), both None
    if either input needed to compute them honestly is missing (no
    entry_price, no opened_at — see trade_outcomes' own nullability notes
    for why those happen) or no candle history covers the window."""
    if entry_price is None or opened_at is None:
        return None, None

    extremes = _candle_extremes(engine, broker, instrument, opened_at, closed_at)
    if extremes is None:
        return None, None
    highest_high, lowest_low = extremes

    direction = 1 if action == "BUY" else -1
    best_price = highest_high if direction > 0 else lowest_low
    worst_price = lowest_low if direction > 0 else highest_high

    mfe_usd = units * direction * (best_price - entry_price)
    mae_usd = units * direction * (worst_price - entry_price)
    # Real bug found live 2026-10-08, verified against 5 real closed Alpaca
    # trades: a real MSFT SELL's realized P&L (-$569.40) came out WORSE than
    # the "worst" MAE this function had just computed from stored candles
    # (-$560.63) -- the actual fill price landed outside the OHLC bar
    # range covering that window (coarser bar boundaries vs. an exact tick
    # price; a genuine data-coverage gap, not a math error). The realized
    # exit is itself a real, certain point on the trade's price path, so
    # MFE/MAE must always be at least as extreme as it, in addition to never
    # crossing 0.
    realized_usd = units * direction * (exit_price - entry_price)
    return max(mfe_usd, realized_usd, 0.0), min(mae_usd, realized_usd, 0.0)


def backfill_mfe_mae(engine: Engine, limit: int | None = None) -> dict[str, int]:
    """Fills mfe_usd/mae_usd for existing trade_outcomes rows that don't
    have them yet. Safe to re-run — only ever touches rows where both
    columns are currently NULL, never recomputes an already-filled row."""
    from sqlalchemy import update

    with engine.connect() as conn:
        query = select(trade_outcomes_table).where(
            trade_outcomes_table.c.mfe_usd.is_(None),
        )
        if limit is not None:
            query = query.limit(limit)
        rows = conn.execute(query).mappings().all()

    updated = 0
    skipped = 0
    for row in rows:
        mfe_usd, mae_usd = compute_mfe_mae(
            engine,
            broker=row["broker"] or "oanda",
            instrument=row["instrument"],
            action=row["action"],
            units=row["units"],
            entry_price=row["entry_price"],
            exit_price=row["exit_price"],
            opened_at=row["opened_at"],
            closed_at=row["closed_at"],
        )
        if mfe_usd is None:
            skipped += 1
            continue
        with engine.begin() as conn:
            conn.execute(
                update(trade_outcomes_table)
                .where(trade_outcomes_table.c.id == row["id"])
                .values(mfe_usd=mfe_usd, mae_usd=mae_usd)
            )
        updated += 1

    return {"updated": updated, "skipped": skipped, "total": len(rows)}

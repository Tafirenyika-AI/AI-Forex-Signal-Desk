"""V4 Priority 2 (docs/V4_ARCHITECTURE.md Section 14 gap): tests for
src/outcomes/excursion.py's MFE/MAE computation.

Uses an isolated in-memory SQLite engine seeded with real-shaped candle rows
-- never the real production DB.

Run: .venv/Scripts/python.exe -m pytest tests/test_v4_excursion.py -v
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from sqlalchemy import create_engine, insert

from src.data.db import candles as candles_table
from src.data.db import metadata
from src.data.db import trade_outcomes as trade_outcomes_table
from src.outcomes.excursion import backfill_mfe_mae, compute_mfe_mae

START = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def engine():
    eng = create_engine("sqlite:///:memory:")
    metadata.create_all(eng)
    return eng


def _seed_candles(engine, broker, instrument, granularity, bars):
    """bars: list of (minutes_offset, high, low)."""
    with engine.begin() as conn:
        for offset, high, low in bars:
            conn.execute(insert(candles_table).values(
                instrument=instrument, granularity=granularity, time=START + timedelta(minutes=offset),
                open=high, high=high, low=low, close=low, volume=100, complete=True, broker=broker,
            ))


def test_long_trade_mfe_is_the_highest_high_above_entry(engine):
    _seed_candles(engine, "alpaca", "AAPL", "M15", [
        (0, 101.0, 99.5),
        (15, 105.0, 100.0),   # best favorable point for a long: high=105
        (30, 102.0, 95.0),    # worst adverse point for a long: low=95
        (45, 103.0, 101.0),
    ])
    mfe, mae = compute_mfe_mae(
        engine, broker="alpaca", instrument="AAPL", action="BUY", units=10,
        entry_price=100.0, exit_price=102.0, opened_at=START, closed_at=START + timedelta(minutes=45),
    )
    assert mfe == pytest.approx((105.0 - 100.0) * 10)
    assert mae == pytest.approx((95.0 - 100.0) * 10)  # negative


def test_short_trade_mfe_mae_directions_are_flipped(engine):
    _seed_candles(engine, "alpaca", "TSLA", "M15", [
        (0, 101.0, 99.5),
        (15, 105.0, 100.0),   # worst adverse point for a short: high=105
        (30, 102.0, 95.0),    # best favorable point for a short: low=95
        (45, 103.0, 101.0),
    ])
    mfe, mae = compute_mfe_mae(
        engine, broker="alpaca", instrument="TSLA", action="SELL", units=10,
        entry_price=100.0, exit_price=98.0, opened_at=START, closed_at=START + timedelta(minutes=45),
    )
    assert mfe == pytest.approx((100.0 - 95.0) * 10)
    assert mae == pytest.approx(-(105.0 - 100.0) * 10)


def test_falls_back_to_h1_when_m15_has_no_rows(engine):
    _seed_candles(engine, "alpaca", "MSFT", "H1", [
        (0, 110.0, 98.0),
    ])
    mfe, mae = compute_mfe_mae(
        engine, broker="alpaca", instrument="MSFT", action="BUY", units=5,
        entry_price=100.0, exit_price=105.0, opened_at=START, closed_at=START + timedelta(hours=1),
    )
    assert mfe == pytest.approx((110.0 - 100.0) * 5)
    assert mae == pytest.approx((98.0 - 100.0) * 5)


def test_missing_entry_price_returns_none_honestly(engine):
    _seed_candles(engine, "alpaca", "NVDA", "M15", [(0, 110.0, 98.0)])
    mfe, mae = compute_mfe_mae(
        engine, broker="alpaca", instrument="NVDA", action="BUY", units=5,
        entry_price=None, exit_price=105.0, opened_at=START, closed_at=START + timedelta(minutes=15),
    )
    assert mfe is None and mae is None


def test_missing_opened_at_returns_none_honestly(engine):
    _seed_candles(engine, "alpaca", "NVDA", "M15", [(0, 110.0, 98.0)])
    mfe, mae = compute_mfe_mae(
        engine, broker="alpaca", instrument="NVDA", action="BUY", units=5,
        entry_price=100.0, exit_price=105.0, opened_at=None, closed_at=START + timedelta(minutes=15),
    )
    assert mfe is None and mae is None


def test_no_candle_coverage_returns_none_honestly(engine):
    mfe, mae = compute_mfe_mae(
        engine, broker="alpaca", instrument="GHOST", action="BUY", units=5,
        entry_price=100.0, exit_price=105.0, opened_at=START, closed_at=START + timedelta(minutes=15),
    )
    assert mfe is None and mae is None


def test_mfe_never_negative_and_mae_never_positive_even_at_the_boundary(engine):
    # Entry price sits slightly outside the stored candle range -- a real,
    # disclosed edge case (different granularity bar boundaries don't
    # perfectly align with the fill timestamp).
    _seed_candles(engine, "alpaca", "AMD", "M15", [(0, 100.5, 99.5)])
    mfe, mae = compute_mfe_mae(
        engine, broker="alpaca", instrument="AMD", action="BUY", units=1,
        entry_price=100.6, exit_price=100.0, opened_at=START, closed_at=START + timedelta(minutes=15),
    )
    assert mfe == 0.0
    assert mae <= 0.0


def test_mae_reflects_realized_pl_when_it_exceeds_the_candle_derived_range(engine):
    # Real bug found live 2026-10-08 against the real production database: a
    # real MSFT SELL's realized P&L (-$569.40) came out WORSE than the
    # "worst" MAE first computed purely from stored candles (-$560.63) --
    # the actual fill landed outside the OHLC bar range covering that
    # window (coarser bars vs. an exact tick price). MFE/MAE must always be
    # at least as extreme as the trade's own known, certain exit.
    _seed_candles(engine, "alpaca", "GME", "M15", [(0, 103.0, 97.0)])
    mfe, mae = compute_mfe_mae(
        engine, broker="alpaca", instrument="GME", action="BUY", units=10,
        # Candle-only mae would be (97-100)*10 = -30; exit is worse (-50).
        entry_price=100.0, exit_price=95.0, opened_at=START, closed_at=START + timedelta(minutes=15),
    )
    assert mfe == pytest.approx((103.0 - 100.0) * 10)  # unaffected -- realized is on the losing side
    assert mae == pytest.approx((95.0 - 100.0) * 10)  # clamped to the realized exit, not the candle low


def test_backfill_fills_only_rows_missing_mfe_and_skips_the_unresolvable(engine):
    _seed_candles(engine, "alpaca", "AAPL", "M15", [
        (0, 105.0, 95.0),
        (15, 103.0, 101.0),
    ])
    now = START + timedelta(minutes=15)
    with engine.begin() as conn:
        conn.execute(insert(trade_outcomes_table).values(
            broker_trade_id="t1", execution_mode="paper", instrument="AAPL", action="BUY",
            units=10, entry_price=100.0, exit_price=103.0, realized_pl_usd=30.0,
            opened_at=START, closed_at=now, outcome="WIN", synced_at=now, broker="alpaca",
        ))
        # Unresolvable: no entry_price -- must be skipped, not crash.
        conn.execute(insert(trade_outcomes_table).values(
            broker_trade_id="t2", execution_mode="paper", instrument="AAPL", action="BUY",
            units=5, entry_price=None, exit_price=103.0, realized_pl_usd=0.0,
            opened_at=START, closed_at=now, outcome="BREAKEVEN", synced_at=now, broker="alpaca",
        ))

    result = backfill_mfe_mae(engine)
    assert result == {"updated": 1, "skipped": 1, "total": 2}

    from sqlalchemy import select
    with engine.connect() as conn:
        rows = {r.broker_trade_id: r for r in conn.execute(select(trade_outcomes_table)).all()}
    assert rows["t1"].mfe_usd == pytest.approx((105.0 - 100.0) * 10)
    assert rows["t2"].mfe_usd is None

    # Re-running must not touch the already-filled row or re-raise on the
    # unresolvable one.
    second_result = backfill_mfe_mae(engine)
    assert second_result == {"updated": 0, "skipped": 1, "total": 1}

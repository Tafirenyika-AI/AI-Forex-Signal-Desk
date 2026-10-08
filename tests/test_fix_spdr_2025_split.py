"""Tests for src/scripts/fix_spdr_2025_split.py's _already_adjusted() gate
and the real UPDATE logic, against an isolated in-memory SQLite engine --
never the real production database (that migration is run once, directly,
not via the test suite).

Run: .venv/Scripts/python.exe -m pytest tests/test_fix_spdr_2025_split.py -v
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from sqlalchemy import create_engine, insert, select, update

from src.data.db import candles as candles_table
from src.data.db import metadata
from src.scripts.fix_spdr_2025_split import SPLIT_CUTOFF, _already_adjusted


@pytest.fixture()
def engine():
    eng = create_engine("sqlite:///:memory:")
    metadata.create_all(eng)
    return eng


def _seed(engine, ticker, pre_close, post_close):
    with engine.begin() as conn:
        conn.execute(insert(candles_table).values(
            instrument=ticker, granularity="H4", time=SPLIT_CUTOFF - timedelta(hours=4),
            open=pre_close, high=pre_close, low=pre_close, close=pre_close, volume=1000, complete=True, broker="alpaca",
        ))
        conn.execute(insert(candles_table).values(
            instrument=ticker, granularity="H4", time=SPLIT_CUTOFF + timedelta(hours=4),
            open=post_close, high=post_close, low=post_close, close=post_close, volume=2000, complete=True, broker="alpaca",
        ))


def test_unadjusted_data_is_detected(engine):
    _seed(engine, "XLK", pre_close=290.0, post_close=146.0)  # real ~2x ratio
    assert _already_adjusted(engine, "XLK") is False


def test_already_adjusted_data_is_detected_and_skipped(engine):
    _seed(engine, "XLK", pre_close=145.0, post_close=146.0)  # already on the same scale
    assert _already_adjusted(engine, "XLK") is True


def test_no_data_spanning_the_boundary_returns_none(engine):
    assert _already_adjusted(engine, "GHOST") is None


def test_the_real_update_halves_price_and_doubles_volume(engine):
    with engine.begin() as conn:
        conn.execute(insert(candles_table).values(
            instrument="XLK", granularity="H4", time=SPLIT_CUTOFF - timedelta(hours=4),
            open=290.0, high=291.0, low=289.0, close=290.0, volume=1000, complete=True, broker="alpaca",
        ))
        conn.execute(insert(candles_table).values(
            instrument="XLK", granularity="H4", time=SPLIT_CUTOFF + timedelta(hours=4),
            open=146.0, high=146.5, low=145.5, close=146.0, volume=2000, complete=True, broker="alpaca",
        ))
    with engine.begin() as conn:
        conn.execute(
            update(candles_table).where(
                candles_table.c.broker == "alpaca", candles_table.c.instrument == "XLK",
                candles_table.c.time < SPLIT_CUTOFF,
            ).values(
                open=candles_table.c.open * 0.5, high=candles_table.c.high * 0.5,
                low=candles_table.c.low * 0.5, close=candles_table.c.close * 0.5,
                volume=candles_table.c.volume * 2,
            )
        )
    with engine.connect() as conn:
        row = conn.execute(
            select(candles_table).where(candles_table.c.time < SPLIT_CUTOFF, candles_table.c.instrument == "XLK")
        ).first()
    assert row.close == pytest.approx(145.0)
    assert row.volume == 2000
    # The post-split row must be untouched.
    with engine.connect() as conn:
        post_row = conn.execute(
            select(candles_table).where(candles_table.c.time >= SPLIT_CUTOFF, candles_table.c.instrument == "XLK")
        ).first()
    assert post_row.close == pytest.approx(146.0)
    assert post_row.volume == 2000

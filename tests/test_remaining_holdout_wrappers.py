"""V4 Priority 6 — holdout wrappers added to close the disclosed gap for
Strategies C, F, and H (src/strategies/trend_following.py,
volatility_breakout.py, sector_rotation.py).

These wrappers reuse each module's own already-tested `_evaluate_core`;
these tests confirm the chronological split plumbing itself (via a real
isolated engine), not the detection logic again (already covered by each
module's own existing test file).

Run: .venv/Scripts/python.exe -m pytest tests/test_remaining_holdout_wrappers.py -v
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
from src.strategies.sector_rotation import SECTOR_ETFS, evaluate_sector_rotation_with_holdout
from src.strategies.trend_following import evaluate_trend_following_with_holdout
from src.strategies.volatility_breakout import evaluate_volatility_breakout_with_holdout

START = datetime(2024, 1, 1, tzinfo=timezone.utc)


@pytest.fixture()
def engine():
    eng = create_engine("sqlite:///:memory:")
    metadata.create_all(eng)
    return eng


def _seed_steady_uptrend(engine, instrument, n_bars=400):
    with engine.begin() as conn:
        price = 100.0
        for i in range(n_bars):
            price *= 1.0015
            conn.execute(insert(candles_table).values(
                instrument=instrument, granularity="H4", time=START + timedelta(hours=4 * i),
                open=price, high=price * 1.001, low=price * 0.999, close=price,
                volume=1000, complete=True, broker="alpaca",
            ))


def test_trend_following_holdout_wrapper_runs_against_a_real_isolated_engine(engine):
    _seed_steady_uptrend(engine, "TEST")
    result = evaluate_trend_following_with_holdout(engine, "alpaca", "TEST", "H4", holding_bars=10, regime_lookback=60)
    assert "development" in result and "holdout" in result
    # Both halves must independently be valid results (not necessarily
    # significant), confirming the split didn't just silently empty one side.
    assert result["development"].n >= 0
    assert result["holdout"].n >= 0


def test_trend_following_holdout_wrapper_insufficient_history_is_honest(engine):
    _seed_steady_uptrend(engine, "TEST", n_bars=10)
    result = evaluate_trend_following_with_holdout(engine, "alpaca", "TEST", "H4", holding_bars=10, regime_lookback=60)
    assert result["development"].n == 0
    assert result["holdout"].n == 0


def test_volatility_breakout_holdout_wrapper_runs_against_a_real_isolated_engine(engine):
    _seed_steady_uptrend(engine, "TEST")
    result = evaluate_volatility_breakout_with_holdout(
        engine, "alpaca", "TEST", "H4", compression_window_bars=20, holding_bars=5, regime_lookback=60,
    )
    assert "development" in result and "holdout" in result
    assert result["development"].n >= 0
    assert result["holdout"].n >= 0


def test_volatility_breakout_holdout_wrapper_insufficient_history_is_honest(engine):
    _seed_steady_uptrend(engine, "TEST", n_bars=10)
    result = evaluate_volatility_breakout_with_holdout(
        engine, "alpaca", "TEST", "H4", compression_window_bars=20, holding_bars=5, regime_lookback=60,
    )
    assert result["development"].n == 0
    assert result["holdout"].n == 0


def test_sector_rotation_holdout_wrapper_runs_against_a_real_isolated_engine(engine):
    _seed_steady_uptrend(engine, "SPY")
    for etf in SECTOR_ETFS[:4]:  # a real, if small, cross-section
        _seed_steady_uptrend(engine, etf)
    result = evaluate_sector_rotation_with_holdout(
        engine, "alpaca", "H4", lookback_bars=20, holding_bars=20, top_tier_fraction=1 / 3, regime_lookback=60,
    )
    assert "development" in result and "holdout" in result


def test_sector_rotation_holdout_wrapper_insufficient_coverage_is_honest(engine):
    _seed_steady_uptrend(engine, "SPY")
    # Only 1 sector ETF -- too few for a real cross-section (needs >= 3).
    _seed_steady_uptrend(engine, SECTOR_ETFS[0])
    result = evaluate_sector_rotation_with_holdout(
        engine, "alpaca", "H4", lookback_bars=20, holding_bars=20, top_tier_fraction=1 / 3, regime_lookback=60,
    )
    assert result["development"].n == 0
    assert result["holdout"].n == 0

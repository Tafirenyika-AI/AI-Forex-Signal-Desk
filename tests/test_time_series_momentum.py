"""Strategy A (src/strategies/registry.py) hypothesis test —
src/strategies/time_series_momentum.py.

Uses an isolated in-memory SQLite engine seeded with deterministic
synthetic candle series (never the real production DB) whose real
momentum/mean-reversion properties are known by construction, so the
function's hit_rate/z_score can be checked against a known-correct answer,
not just "doesn't crash."

Run: .venv/Scripts/python.exe -m pytest tests/test_time_series_momentum.py -v
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
from src.strategies.time_series_momentum import evaluate_momentum_hypothesis

START = datetime(2020, 1, 1, tzinfo=timezone.utc)


@pytest.fixture()
def engine():
    eng = create_engine("sqlite:///:memory:")
    metadata.create_all(eng)
    return eng


def _seed_daily_closes(engine, closes, instrument="TEST", broker="alpaca"):
    with engine.begin() as conn:
        for i, c in enumerate(closes):
            conn.execute(insert(candles_table).values(
                instrument=instrument, granularity="D", time=START + timedelta(days=i),
                open=c, high=c, low=c, close=c, volume=100, complete=True, broker=broker,
            ))


def _persistent_series(n_blocks=5, block_days=40, daily_move=0.01):
    price = 100.0
    closes = []
    for b in range(n_blocks):
        direction = 1 if b % 2 == 0 else -1
        for _ in range(block_days):
            price *= 1 + direction * daily_move
            closes.append(price)
    return closes


def _anti_persistent_series(n_days=200, period_days=10, daily_move=0.01):
    price = 100.0
    closes = []
    for i in range(n_days):
        direction = 1 if (i % period_days) < period_days // 2 else -1
        price *= 1 + direction * daily_move
        closes.append(price)
    return closes


def test_genuinely_persistent_series_shows_a_strong_positive_momentum_signal(engine):
    # Known by construction: long alternating up/down blocks (40 days each)
    # mean a 5-day trailing window almost always shares the same regime
    # (and therefore sign) as the following 5-day forward window.
    _seed_daily_closes(engine, _persistent_series())
    result = evaluate_momentum_hypothesis(engine, "alpaca", "TEST", "D", lookback_days=5, holding_days=5)
    assert result.n > 150
    assert result.hit_rate > 0.8
    assert result.z_score > 5  # overwhelmingly significant, by construction


def test_genuinely_anti_persistent_series_shows_a_strong_negative_momentum_signal(engine):
    # Known by construction: a 10-day triangle wave means a 5-day trailing
    # window is reliably the OPPOSITE half-cycle from the following 5-day
    # forward window.
    _seed_daily_closes(engine, _anti_persistent_series())
    result = evaluate_momentum_hypothesis(engine, "alpaca", "TEST", "D", lookback_days=5, holding_days=5)
    assert result.n > 150
    assert result.hit_rate < 0.2
    assert result.z_score < -5


def test_insufficient_history_returns_honest_none_fields(engine):
    _seed_daily_closes(engine, [100.0, 101.0, 102.0])  # far too little history
    result = evaluate_momentum_hypothesis(engine, "alpaca", "TEST", "D", lookback_days=5, holding_days=5)
    assert result.n == 0
    assert result.hit_rate is None
    assert result.z_score is None
    assert result.mean_move_in_favor is None


def test_noise_floor_filters_out_small_trailing_moves(engine):
    # Small, constant day-to-day wiggle throughout (every row has a real but
    # tiny nonzero trailing return -- the loose filter counts all of them),
    # plus one sustained, genuinely large 10-day move block (only that
    # block clears a strict noise floor).
    price = 100.0
    closes = []
    for i in range(120):
        move = 0.0005 if i % 2 == 0 else -0.0005
        if 50 <= i < 60:
            move = 0.02
        price *= 1 + move
        closes.append(price)
    _seed_daily_closes(engine, closes)
    loose = evaluate_momentum_hypothesis(
        engine, "alpaca", "TEST", "D", lookback_days=5, holding_days=5, entry_noise_floor_std=0.0,
    )
    strict = evaluate_momentum_hypothesis(
        engine, "alpaca", "TEST", "D", lookback_days=5, holding_days=5, entry_noise_floor_std=3.0,
    )
    assert strict.n < loose.n
    assert strict.n > 0

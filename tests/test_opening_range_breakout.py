"""Strategy D (src/strategies/registry.py) hypothesis test —
src/strategies/opening_range_breakout.py.

Tests `_evaluate_core` directly against fabricated per-day DataFrames
(high/low/close/volume given outright), same split-core pattern as the
other strategy modules this session.

Run: .venv/Scripts/python.exe -m pytest tests/test_opening_range_breakout.py -v
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import pytest

from sqlalchemy import create_engine, insert

from src.data.db import candles as candles_table
from src.data.db import metadata
from src.strategies.opening_range_breakout import _evaluate_core, evaluate_opening_range_breakout_with_holdout

DAY_START = datetime(2026, 1, 5, 13, 30, tzinfo=timezone.utc)


def _make_day(bars):
    """bars: list of (minutes_offset, high, low, close, volume)."""
    rows = [
        {"time": DAY_START + timedelta(minutes=m), "high": h, "low": l, "close": c, "volume": v}
        for m, h, l, c, v in bars
    ]
    return pd.DataFrame(rows)


def test_persistent_breakout_up_shows_a_positive_signal():
    days = []
    for _ in range(10):
        day = _make_day([
            (0, 101.0, 99.0, 100.5, 1000),   # opening range bar 1
            (15, 101.5, 100.0, 101.0, 1000),  # opening range bar 2 -- range high=101.5, low=99.0, avg_vol=1000
            (30, 103.0, 101.5, 102.5, 2000),  # breaks above 101.5 on high volume -- qualifying breakout up
            (45, 104.0, 102.0, 103.5, 1500),
            (60, 105.0, 103.0, 104.5, 1200),  # final close of day: 104.5, well above breakout close 102.5
        ])
        days.append(day)
    n, hit_rate, z_score, mean_move = _evaluate_core(days, opening_range_bars=2, volume_multiple=1.2)
    assert n == 10
    assert hit_rate == 1.0
    assert mean_move > 0


def test_false_breakout_reversing_shows_a_negative_signal():
    days = []
    for _ in range(10):
        day = _make_day([
            (0, 101.0, 99.0, 100.5, 1000),
            (15, 101.5, 100.0, 101.0, 1000),
            (30, 103.0, 101.5, 102.5, 2000),  # breaks up on volume...
            (45, 101.0, 99.0, 100.0, 1500),   # ...but reverses hard
            (60, 99.0, 97.0, 98.0, 1200),     # final close well BELOW the breakout close
        ])
        days.append(day)
    n, hit_rate, z_score, mean_move = _evaluate_core(days, opening_range_bars=2, volume_multiple=1.2)
    assert n == 10
    assert hit_rate == 0.0
    assert mean_move < 0


def test_low_volume_breakout_does_not_qualify():
    days = [_make_day([
        (0, 101.0, 99.0, 100.5, 1000),
        (15, 101.5, 100.0, 101.0, 1000),
        (30, 103.0, 101.5, 102.5, 900),  # breaks the range but on LOW volume -- must not qualify
        (45, 104.0, 102.0, 103.5, 900),
    ])]
    n, hit_rate, z_score, mean_move = _evaluate_core(days, opening_range_bars=2, volume_multiple=1.2)
    assert n == 0


def test_day_with_too_few_bars_is_skipped():
    days = [_make_day([(0, 101.0, 99.0, 100.5, 1000), (15, 101.5, 100.0, 101.0, 1000)])]  # no bars after the opening range
    n, hit_rate, z_score, mean_move = _evaluate_core(days, opening_range_bars=2, volume_multiple=1.2)
    assert n == 0
    assert hit_rate is None and z_score is None and mean_move is None


def test_holdout_wrapper_against_a_real_isolated_engine():
    eng = create_engine("sqlite:///:memory:")
    metadata.create_all(eng)
    # 20 real trading days, each with a clean, persistent breakout up --
    # should replicate in both development and holdout.
    with eng.begin() as conn:
        for day_idx in range(20):
            day_start = DAY_START + timedelta(days=day_idx)
            bars = [
                (0, 101.0, 99.0, 100.5, 1000), (15, 101.5, 100.0, 101.0, 1000),
                (30, 103.0, 101.5, 102.5, 2000), (45, 104.0, 102.0, 103.5, 1500),
                (60, 105.0, 103.0, 104.5, 1200),
            ]
            for m, h, l, c, v in bars:
                conn.execute(insert(candles_table).values(
                    instrument="TEST", granularity="M15", time=day_start + timedelta(minutes=m),
                    open=c, high=h, low=l, close=c, volume=v, complete=True, broker="alpaca",
                ))
    result = evaluate_opening_range_breakout_with_holdout(eng, "alpaca", "TEST", opening_range_bars=2, volume_multiple=1.2)
    assert result["development"].hit_rate == 1.0
    assert result["holdout"].hit_rate == 1.0

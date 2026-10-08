"""Strategy B (src/strategies/registry.py) hypothesis test —
src/strategies/cross_sectional_momentum.py.

Tests `_evaluate_core` directly against fabricated trailing/forward
absolute-return frames, same split-core pattern as sector_rotation.py's
own tests.

Run: .venv/Scripts/python.exe -m pytest tests/test_cross_sectional_momentum.py -v
"""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np
import pandas as pd
import pytest

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, insert

from src.data.db import candles as candles_table
from src.data.db import metadata
from src.strategies.cross_sectional_momentum import _evaluate_core, evaluate_cross_sectional_momentum_with_holdout


def test_persistent_winners_and_losers_show_a_positive_spread():
    idx = range(10)
    # A is always the top trailing performer and the top forward performer;
    # F is always the bottom of both -- a small per-row wobble on the
    # forward side gives a real (nonzero), realistic spread variance so
    # the t-statistic is actually computable, not just a degenerate
    # zero-variance case.
    wobble = [0.002 * (i % 3 - 1) for i in range(10)]
    trailing = pd.DataFrame({
        "A": [0.05] * 10, "B": [0.02] * 10, "C": [0.0] * 10,
        "D": [-0.01] * 10, "E": [-0.02] * 10, "F": [-0.05] * 10,
    }, index=idx)
    forward = pd.DataFrame({
        "A": [0.03 + w for w in wobble], "B": [0.0] * 10, "C": [0.0] * 10,
        "D": [0.0] * 10, "E": [0.0] * 10, "F": [-0.02 - w for w in wobble],
    }, index=idx)

    result = _evaluate_core(trailing, forward, top_tier_fraction=1 / 6)
    assert result.n == 10
    assert result.mean_spread_return == pytest.approx(0.05, abs=0.001)
    assert result.t_statistic is not None and result.t_statistic > 0


def test_rows_with_any_nan_are_skipped():
    idx = range(3)
    trailing = pd.DataFrame({"A": [0.05, np.nan, 0.05], "B": [0.0, 0.0, 0.0], "C": [-0.02, -0.02, -0.02]}, index=idx)
    forward = pd.DataFrame({"A": [0.02, 0.02, np.nan], "B": [0.0, 0.0, 0.0], "C": [0.0, 0.0, 0.0]}, index=idx)

    result = _evaluate_core(trailing, forward, top_tier_fraction=1 / 3)
    assert result.n == 1  # only row 0 is fully clean


def test_insufficient_observations_returns_honest_none_fields():
    idx = range(1)
    trailing = pd.DataFrame({"A": [0.05], "B": [0.0], "C": [-0.02]}, index=idx)
    forward = pd.DataFrame({"A": [0.02], "B": [0.0], "C": [0.0]}, index=idx)

    result = _evaluate_core(trailing, forward, top_tier_fraction=1 / 3)
    assert result.mean_spread_return is None
    assert result.t_statistic is None


def test_holdout_split_against_a_real_isolated_engine():
    eng = create_engine("sqlite:///:memory:")
    metadata.create_all(eng)
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    instruments = ["A", "B", "C", "D", "E", "F"]
    with eng.begin() as conn:
        for name_idx, instrument in enumerate(instruments):
            price = 100.0
            drift = 0.002 * (len(instruments) // 2 - name_idx)  # A drifts up most, F down most, consistently
            for day in range(120):
                price *= 1 + drift
                conn.execute(insert(candles_table).values(
                    instrument=instrument, granularity="D", time=start + timedelta(days=day),
                    open=price, high=price, low=price, close=price, volume=100, complete=True, broker="alpaca",
                ))
    result = evaluate_cross_sectional_momentum_with_holdout(
        eng, "alpaca", tuple(instruments), "D", lookback_bars=5, holding_bars=5, holdout_fraction=0.2,
    )
    # A consistent, uniform drift ranking should replicate in both halves.
    assert result["development"].mean_spread_return is not None
    assert result["holdout"].mean_spread_return is not None
    assert result["development"].mean_spread_return > 0
    assert result["holdout"].mean_spread_return > 0

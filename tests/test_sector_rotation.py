"""Strategy H (src/strategies/registry.py) hypothesis test —
src/strategies/sector_rotation.py.

Tests `_evaluate_core` directly against fabricated trailing/forward
relative-return frames (bypassing real candle loading + classify_regime
round trips), same pattern as tests/test_volatility_breakout.py and
tests/test_trend_following.py.

Run: .venv/Scripts/python.exe -m pytest tests/test_sector_rotation.py -v
"""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import pytest

from src.strategies.sector_rotation import _evaluate_core


def test_persistent_top_tier_relative_strength_shows_positive_mean_forward_return():
    # 6 ETFs, 10 timestamps. ETF "A" is always the top trailing performer
    # and always continues to outperform going forward too.
    idx = range(10)
    trailing = pd.DataFrame({
        "A": [0.05] * 10, "B": [0.01] * 10, "C": [0.0] * 10,
        "D": [-0.01] * 10, "E": [-0.02] * 10, "F": [-0.03] * 10,
    }, index=idx)
    forward = pd.DataFrame({
        "A": [0.03] * 10, "B": [0.0] * 10, "C": [0.0] * 10,
        "D": [0.0] * 10, "E": [0.0] * 10, "F": [0.0] * 10,
    }, index=idx)
    not_shock = pd.Series([True] * 10, index=idx)

    result = _evaluate_core(trailing, forward, not_shock, top_tier_fraction=1 / 6)
    assert result.n == 10  # top-1-of-6 selected every eligible timestamp
    assert result.mean_forward_relative_return == pytest.approx(0.03)
    assert result.t_statistic is not None and result.t_statistic > 0


def test_shock_timestamps_are_excluded():
    idx = range(4)
    trailing = pd.DataFrame({"A": [0.05] * 4, "B": [0.0] * 4}, index=idx)
    forward = pd.DataFrame({"A": [0.05] * 4, "B": [0.0] * 4}, index=idx)
    not_shock = pd.Series([True, False, True, False], index=idx)

    result = _evaluate_core(trailing, forward, not_shock, top_tier_fraction=0.5)
    assert result.n == 2  # only the 2 non-SHOCK timestamps count


def test_rows_with_any_nan_trailing_or_forward_value_are_skipped():
    import numpy as np
    idx = range(3)
    # Row 0: clean (counts). Row 1: NaN trailing (skipped entirely before
    # even picking a top tier). Row 2: clean trailing but the top-tier
    # pick's own forward value is NaN (skipped after picking, not before).
    trailing = pd.DataFrame({"A": [0.05, np.nan, 0.05], "B": [0.0, 0.0, 0.0]}, index=idx)
    forward = pd.DataFrame({"A": [0.02, 0.02, np.nan], "B": [0.0, 0.0, 0.0]}, index=idx)
    not_shock = pd.Series([True, True, True], index=idx)

    result = _evaluate_core(trailing, forward, not_shock, top_tier_fraction=0.5)
    assert result.n == 1  # only row 0 is fully clean


def test_insufficient_observations_returns_honest_none_fields():
    idx = range(1)
    trailing = pd.DataFrame({"A": [0.05], "B": [0.0]}, index=idx)
    forward = pd.DataFrame({"A": [0.02], "B": [0.0]}, index=idx)
    not_shock = pd.Series([True], index=idx)

    result = _evaluate_core(trailing, forward, not_shock, top_tier_fraction=0.5)
    assert result.mean_forward_relative_return is None
    assert result.t_statistic is None

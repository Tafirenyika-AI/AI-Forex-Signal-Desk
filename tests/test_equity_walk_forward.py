"""Unit tests for Equity V2 Phase 11: src/backtest/equity_walk_forward.py.

Run: .venv/Scripts/python.exe -m pytest tests/test_equity_walk_forward.py -v
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

from src.backtest.equity_walk_forward import _aggregate_metrics, _brier, _simple_returns_at, run_equity_walk_forward
from src.features.engine import FEATURE_COLUMNS, REGIME_FEATURE_COLUMNS
from src.models.equity_challengers import COMPONENT_COLUMNS


def test_simple_returns_at_uses_the_reference_frame_not_the_filtered_subset():
    # Real bug this guards against: computing .shift(-horizon_bars)
    # directly on a gappy (filtered) subset would look up the WRONG
    # future bar. Build a full_df of 10 sequential bars, then request
    # returns for a gappy index (skipping row 1) -- the result for row 0
    # must still be computed against row 0's REAL 2-bars-ahead neighbor
    # (row 2) in full_df, not against whatever row happens to be 2
    # positions later in the gappy subset.
    full_df = pd.DataFrame({"close": [100.0, 999.0, 110.0, 120.0, 130.0, 140.0, 150.0, 160.0, 170.0, 180.0]})
    gappy_index = pd.Index([0, 2, 3])  # row 1 (close=999) deliberately excluded
    probs = np.array([0.9, 0.9, 0.9])  # always "long"
    returns = _simple_returns_at(full_df, gappy_index, probs, horizon_bars=2)
    # row 0's real 2-bars-ahead close is row 2 (110.0), NOT row 3 (which a
    # naive shift on the gappy 3-row subset would have wrongly picked).
    expected_row0 = (110.0 - 100.0) / 100.0
    assert returns[0] == pytest.approx(expected_row0)


def test_aggregate_metrics_empty_returns_nan_not_crash():
    result = _aggregate_metrics("x", np.array([]), np.array([]), np.array([]), None)
    assert result.n_oos_predictions == 0
    assert np.isnan(result.expectancy)


def test_aggregate_metrics_regime_stability_breakdown():
    returns = np.array([0.01, -0.02, 0.03, 0.01])
    probs = np.array([0.6, 0.4, 0.7, 0.55])
    actuals = np.array([1, 0, 1, 1])
    regimes = np.array(["TREND", "RANGE", "TREND", "RANGE"])
    result = _aggregate_metrics("x", returns, probs, actuals, regimes)
    assert set(result.regime_stability.keys()) == {"TREND", "RANGE"}
    assert result.regime_stability["TREND"]["n"] == 2


def test_brier_perfect_prediction_is_zero():
    assert _brier(np.array([1.0, 0.0]), np.array([1.0, 0.0])) == pytest.approx(0.0)


def test_brier_constant_half_guess_is_quarter():
    assert _brier(np.array([0.5, 0.5, 0.5, 0.5]), np.array([1.0, 0.0, 1.0, 0.0])) == pytest.approx(0.25)


def _synthetic_training_frame(n: int = 400, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 0.5, n))
    df = pd.DataFrame({
        "time": pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC"),
        "close": close, "open": close - 0.1, "high": close + 0.3, "low": close - 0.3, "volume": 1000.0,
    })
    for col in FEATURE_COLUMNS:
        if col not in df:
            df[col] = rng.normal(0, 1, n)
    for col in REGIME_FEATURE_COLUMNS:
        df[col] = rng.uniform(0, 1, n)
    df["regime"] = rng.choice(["TREND", "RANGE", "SHOCK"], n)
    for columns in COMPONENT_COLUMNS.values():
        for col in columns:
            if col not in df:
                df[col] = rng.normal(0, 1, n)
    df["target_up"] = rng.integers(0, 2, n)
    return df


def test_run_equity_walk_forward_end_to_end_no_crash_and_sane_shape():
    frame = _synthetic_training_frame(n=400)
    results = run_equity_walk_forward(frame, horizon_bars=4, train_window=150, test_window=50)
    expected_names = {"price", "meta", *COMPONENT_COLUMNS.keys()}
    assert set(results.keys()) == expected_names
    for name, result in results.items():
        if result.n_oos_predictions > 0:
            assert not np.isnan(result.brier_score)
            assert 0.0 <= result.brier_score <= 1.0

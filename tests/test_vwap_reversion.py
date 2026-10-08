"""Strategy E (src/strategies/registry.py) hypothesis test —
src/strategies/vwap_reversion.py.

Tests `_evaluate_core` directly against a fabricated classified-style
DataFrame (regime/vwap_distance/close given outright), same pattern as
tests/test_volatility_breakout.py and tests/test_trend_following.py.

Run: .venv/Scripts/python.exe -m pytest tests/test_vwap_reversion.py -v
"""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import pytest

from src.strategies.vwap_reversion import _evaluate_core, evaluate_vwap_reversion_with_holdout


def _build(regime, vwap_distance, closes):
    return pd.DataFrame({"regime": regime, "vwap_distance": vwap_distance, "close": closes})


def test_overbought_range_regime_reverts_down_shows_positive_signal():
    n = 30
    regime = ["RANGE"] * n
    vwap_distance = [0.001] * (n - 10) + [0.05] * 10  # stretched far above VWAP for the last 10 bars
    closes = [100.0] * (n - 10) + [105.0 - i * 0.5 for i in range(10)]  # then reverts down
    classified = _build(regime, vwap_distance, closes)
    n_result, hit_rate, z_score, mean_move = _evaluate_core(classified, deviation_std_threshold=1.0, holding_bars=5)
    assert n_result > 0
    assert hit_rate > 0.5
    assert mean_move > 0


def test_trend_regime_is_excluded_even_with_a_stretched_vwap():
    n = 30
    regime = ["TREND"] * n  # not RANGE -- must never qualify, no matter how stretched
    vwap_distance = [0.05] * n
    closes = [100.0 + i for i in range(n)]
    classified = _build(regime, vwap_distance, closes)
    n_result, hit_rate, z_score, mean_move = _evaluate_core(classified, deviation_std_threshold=1.0, holding_bars=5)
    assert n_result == 0


def test_small_deviation_below_threshold_does_not_qualify():
    n = 30
    regime = ["RANGE"] * n
    # Small oscillation with a real, nonzero std -- every value stays
    # within 3 std of itself, so nothing should qualify as "stretched."
    vwap_distance = [0.0001 if i % 2 == 0 else -0.0001 for i in range(n)]
    closes = [100.0] * n
    classified = _build(regime, vwap_distance, closes)
    n_result, hit_rate, z_score, mean_move = _evaluate_core(classified, deviation_std_threshold=3.0, holding_bars=5)
    assert n_result == 0


def test_no_qualifying_rows_returns_honest_none_fields():
    classified = _build(["TREND"] * 10, [0.0] * 10, [100.0] * 10)
    n_result, hit_rate, z_score, mean_move = _evaluate_core(classified, deviation_std_threshold=2.0, holding_bars=5)
    assert n_result == 0
    assert hit_rate is None and z_score is None and mean_move is None


def test_holdout_wrapper_returns_honest_empties_with_insufficient_history():
    import os as _os
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import create_engine, insert

    from src.data.db import candles as candles_table
    from src.data.db import metadata

    eng = create_engine("sqlite:///:memory:")
    metadata.create_all(eng)
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    with eng.begin() as conn:
        for i in range(5):  # far too little history for regime_lookback=250
            conn.execute(insert(candles_table).values(
                instrument="TEST", granularity="H1", time=start + timedelta(hours=i),
                open=100.0, high=100.0, low=100.0, close=100.0, volume=100, complete=True, broker="alpaca",
            ))
    result = evaluate_vwap_reversion_with_holdout(eng, "alpaca", "TEST", "H1")
    assert result["development"].n == 0
    assert result["holdout"].n == 0

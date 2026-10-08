"""Strategy F (src/strategies/registry.py) hypothesis test —
src/strategies/volatility_breakout.py.

Tests `_evaluate_core` directly against a fabricated classified-style
DataFrame (regime_low_volatility/vol_percentile/log_return_1/close given
outright) rather than round-tripping through compute_features()/
classify_regime() with a hand-built OHLC series -- classify_regime()'s own
trailing percentiles are measured against a long window, which made an
end-to-end fixture fragile and indirect (confirmed the hard way: an
initial end-to-end attempt produced bizarre results because the hand-built
price series interacted with the rolling percentile window in unintended
ways). classify_regime() already has its own dedicated tests
(tests/test_regime_v4_detail_columns.py).

Run: .venv/Scripts/python.exe -m pytest tests/test_volatility_breakout.py -v
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

from src.strategies.volatility_breakout import _evaluate_core


def _build_classified(persistent: bool, compression_bars=20, hold_bars=5, tail_bars=10):
    rows = []
    price = 100.0
    # Compression: low_vol True, small log_return_1 throughout.
    for _ in range(compression_bars):
        price *= 1.0001
        rows.append({"regime_low_volatility": True, "vol_percentile": 0.1, "log_return_1": 0.0001, "close": price})
    # Expansion trigger bar: low_vol False, vol_percentile jumps, a real +3% move.
    price *= 1.03
    rows.append({"regime_low_volatility": False, "vol_percentile": 0.9, "log_return_1": 0.03, "close": price})
    # Hold bars: still "expanding" (vol_percentile stays elevated, same as
    # a real expansion episode would) -- this is exactly what the
    # rising-edge detector must collapse into a single event, not hold_bars
    # separate ones.
    move = 0.01 if persistent else -0.01
    for _ in range(hold_bars):
        price *= 1 + move
        rows.append({"regime_low_volatility": False, "vol_percentile": 0.9, "log_return_1": move, "close": price})
    # Tail: back to normal, irrelevant filler so forward-return lookups have somewhere to land.
    for _ in range(tail_bars):
        price *= 1.0001
        rows.append({"regime_low_volatility": False, "vol_percentile": 0.5, "log_return_1": 0.0001, "close": price})
    return pd.DataFrame(rows)


def test_persistent_expansion_shows_a_positive_momentum_signal():
    classified = _build_classified(persistent=True)
    n, hit_rate, z_score, mean_move = _evaluate_core(
        classified, compression_window_bars=10, expansion_vol_threshold=0.5, holding_bars=5,
    )
    assert n == 1  # exactly one event -- the rising edge, not one per hold bar
    assert hit_rate == 1.0
    assert mean_move > 0


def test_reversing_expansion_shows_a_negative_momentum_signal():
    classified = _build_classified(persistent=False)
    n, hit_rate, z_score, mean_move = _evaluate_core(
        classified, compression_window_bars=10, expansion_vol_threshold=0.5, holding_bars=5,
    )
    assert n == 1
    assert hit_rate == 0.0
    assert mean_move < 0


def test_rising_edge_detector_collapses_a_multi_bar_expansion_into_one_event():
    # The core regression this module's own real bug was about: without the
    # edge detector, every one of the hold_bars would also independently
    # "qualify" (vol_percentile stays elevated throughout), producing
    # hold_bars+1 events instead of exactly 1.
    classified = _build_classified(persistent=True, hold_bars=8)
    n, *_ = _evaluate_core(classified, compression_window_bars=10, expansion_vol_threshold=0.5, holding_bars=5)
    assert n == 1


def test_no_qualifying_events_returns_honest_none_fields():
    # Never compressed at all -- nothing should ever qualify.
    rows = [
        {"regime_low_volatility": False, "vol_percentile": 0.6, "log_return_1": 0.001, "close": 100.0 * (1.001 ** i)}
        for i in range(50)
    ]
    classified = pd.DataFrame(rows)
    n, hit_rate, z_score, mean_move = _evaluate_core(
        classified, compression_window_bars=10, expansion_vol_threshold=0.5, holding_bars=5,
    )
    assert n == 0
    assert hit_rate is None and z_score is None and mean_move is None

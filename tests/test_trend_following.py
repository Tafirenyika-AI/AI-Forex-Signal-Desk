"""Strategy C (src/strategies/registry.py) hypothesis test —
src/strategies/trend_following.py.

Tests `_evaluate_core` directly against a fabricated classified-style
DataFrame (regime/regime_direction/close given outright), same pattern as
tests/test_volatility_breakout.py and for the same reason: classify_regime()
already has its own dedicated tests, and this module's own entry-detection
logic is what actually needs proving here.

Run: .venv/Scripts/python.exe -m pytest tests/test_trend_following.py -v
"""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import pytest

from src.strategies.trend_following import _evaluate_core


def _build_classified(direction: str, persistent: bool, trend_bars=5, tail_bars=30):
    rows = []
    price = 100.0
    # Pre-trend: RANGE regime, flat-ish.
    for _ in range(10):
        price *= 1.0001
        rows.append({"regime": "RANGE", "regime_direction": None, "close": price})
    move_sign = 1 if direction == "TREND_UP" else -1
    for _ in range(trend_bars):
        price *= 1 + move_sign * 0.005
        rows.append({"regime": "TREND", "regime_direction": direction, "close": price})
    # After the trend "ends" (regime reverts), what actually happens to
    # price over the next holding_bars is what the test controls.
    tail_move = move_sign * 0.003 if persistent else -move_sign * 0.003
    for _ in range(tail_bars):
        price *= 1 + tail_move
        rows.append({"regime": "RANGE", "regime_direction": None, "close": price})
    return pd.DataFrame(rows)


def test_persistent_uptrend_entered_fresh_shows_positive_signal():
    # holding_bars (20) deliberately exceeds trend_bars (5, the default) so
    # the forward-return window lands in the post-trend "tail" region,
    # which is what persistent/reversing actually controls -- not still
    # inside the trend itself.
    classified = _build_classified("TREND_UP", persistent=True)
    n, hit_rate, z_score, mean_move = _evaluate_core(classified, holding_bars=20)
    assert n == 1  # only the FIRST trend bar counts as a fresh transition
    assert hit_rate == 1.0
    assert mean_move > 0


def test_reversing_downtrend_entered_fresh_shows_negative_signal():
    classified = _build_classified("TREND_DOWN", persistent=False)
    n, hit_rate, z_score, mean_move = _evaluate_core(classified, holding_bars=20)
    assert n == 1
    assert hit_rate == 0.0
    assert mean_move < 0


def test_only_the_first_bar_of_a_long_trend_counts_not_every_bar_inside_it():
    classified = _build_classified("TREND_UP", persistent=True, trend_bars=40)
    n, *_ = _evaluate_core(classified, holding_bars=10)
    assert n == 1


def test_no_trend_ever_returns_honest_none_fields():
    rows = [{"regime": "RANGE", "regime_direction": None, "close": 100.0 * (1.0001 ** i)} for i in range(50)]
    classified = pd.DataFrame(rows)
    n, hit_rate, z_score, mean_move = _evaluate_core(classified, holding_bars=10)
    assert n == 0
    assert hit_rate is None and z_score is None and mean_move is None

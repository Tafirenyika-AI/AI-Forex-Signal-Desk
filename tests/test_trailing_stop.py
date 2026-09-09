"""Unit tests for src/execution/trailing_stop.py's compute_new_stop —
the live-cycle counterpart to src/backtest/engine.py's already-tested
_simulate_trailing_exit ratchet math.

Run: .venv/Scripts/python.exe -m pytest tests/test_trailing_stop.py -v
"""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

from src.execution.trailing_stop import compute_new_stop


def test_long_meaningful_favorable_move_returns_new_stop():
    # price rallied to 110, atr=2, multiplier=1.5 -> candidate = 107
    # current stop is 95 -> candidate clears min_move (0.25*2=0.5) easily
    new_stop = compute_new_stop(
        direction=1, current_price=110, current_stop_price=95, atr_14=2, atr_multiplier=1.5,
    )
    assert new_stop == 107


def test_short_meaningful_favorable_move_returns_new_stop():
    new_stop = compute_new_stop(
        direction=-1, current_price=90, current_stop_price=105, atr_14=2, atr_multiplier=1.5,
    )
    assert new_stop == 93


def test_long_tiny_move_below_threshold_returns_none():
    # candidate is only slightly above current stop -- less than min_move (0.5)
    new_stop = compute_new_stop(
        direction=1, current_price=100, current_stop_price=97.7, atr_14=2, atr_multiplier=1.5,
        min_move_atr_fraction=0.25,
    )
    assert new_stop is None


def test_long_never_loosens_the_stop():
    # a pullback lowers the candidate below the current stop -- must not move
    new_stop = compute_new_stop(
        direction=1, current_price=95, current_stop_price=100, atr_14=2, atr_multiplier=1.5,
    )
    assert new_stop is None


def test_short_never_loosens_the_stop():
    new_stop = compute_new_stop(
        direction=-1, current_price=105, current_stop_price=100, atr_14=2, atr_multiplier=1.5,
    )
    assert new_stop is None


def test_zero_or_negative_atr_returns_none():
    assert compute_new_stop(direction=1, current_price=110, current_stop_price=95, atr_14=0) is None
    assert compute_new_stop(direction=1, current_price=110, current_stop_price=95, atr_14=-1) is None

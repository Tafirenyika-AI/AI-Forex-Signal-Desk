"""V4 Priority 6 — src/data/corporate_actions.py.

Run: .venv/Scripts/python.exe -m pytest tests/test_corporate_actions.py -v
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd

from src.data.corporate_actions import detect_likely_stock_splits

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _series(closes):
    times = pd.Series([START + timedelta(days=i) for i in range(len(closes))])
    return times, pd.Series(closes)


def test_a_real_2_for_1_split_is_detected():
    closes = [100.0] * 10 + [50.0] + [50.5] * 5  # halves cleanly -- a real 2-for-1 split
    times, close_series = _series(closes)
    result = detect_likely_stock_splits("TEST", times, close_series)
    assert len(result) == 1
    assert result[0].likely_split == "2-for-1"
    assert result[0].ratio < 1.0  # the raw ratio (post/pre) for a forward split is < 1


def test_a_real_10_for_1_split_is_detected():
    closes = [1000.0] * 5 + [100.0] + [101.0] * 5
    times, close_series = _series(closes)
    result = detect_likely_stock_splits("TEST", times, close_series)
    assert len(result) == 1
    assert result[0].likely_split == "10-for-1"


def test_a_large_but_non_split_move_is_not_flagged():
    # The project's own real April 2025 NVDA example: a genuine +15.9%
    # single-bar rally, which matches no common split ratio.
    closes = [100.0] * 10 + [115.87] + [116.0] * 5
    times, close_series = _series(closes)
    result = detect_likely_stock_splits("TEST", times, close_series)
    assert result == []


def test_small_moves_are_never_flagged_even_if_coincidentally_near_a_ratio():
    closes = [100.0, 101.0, 99.0, 100.5]  # normal daily noise, nowhere near any split ratio
    times, close_series = _series(closes)
    result = detect_likely_stock_splits("TEST", times, close_series)
    assert result == []


def test_reverse_split_is_detected():
    closes = [10.0] * 5 + [100.0] + [101.0] * 5  # a real 1-for-10 reverse split
    times, close_series = _series(closes)
    result = detect_likely_stock_splits("TEST", times, close_series)
    assert len(result) == 1
    assert result[0].likely_split == "1-for-10"

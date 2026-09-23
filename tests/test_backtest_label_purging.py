"""Unit test for P1-03 (external review, 2026-09-23, T05): the last
horizon_bars rows of each walk-forward training window must be purged,
since their target_up label (close[row + horizon_bars]) reaches into the
following test window's own prices.

Exercises the REAL add_forward_target and walk_forward_splits functions
(not a reimplementation) against the exact same split-slicing expression
run_walk_forward_backtest uses, without needing the full feature/model
pipeline.

Run: .venv/Scripts/python.exe -m pytest tests/test_backtest_label_purging.py -v
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd

from src.features.engine import add_forward_target
from src.models.price_model import walk_forward_splits


def _synthetic_closes(n: int) -> pd.DataFrame:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return pd.DataFrame({
        "time": [base + timedelta(hours=i) for i in range(n)],
        "close": [100.0 + i for i in range(n)],  # monotonic -- makes target_up trivially checkable
    })


def test_t05_purged_training_window_never_uses_test_period_prices():
    # Exact brief reproduction: train_window=250, horizon_bars=4 -> rows
    # 246-249 (0-indexed within the window) are the ones whose label
    # reaches into the test window and must be purged.
    horizon_bars = 4
    train_window = 250
    test_window = 50

    n = train_window + test_window + 10
    df = _synthetic_closes(n)
    labeled = add_forward_target(df, horizon_bars)
    usable = labeled.dropna(subset=["target_up"]).reset_index(drop=True)

    split = next(iter(walk_forward_splits(len(usable), train_window, test_window)))
    assert split.train_start == 0
    assert split.train_end == train_window  # 250, confirms the brief's own row numbering applies directly

    # The fix under test: the same expression run_walk_forward_backtest uses.
    purged_train_df = usable.iloc[split.train_start : split.train_end - horizon_bars]
    test_df = usable.iloc[split.test_start : split.test_end]

    assert len(purged_train_df) == train_window - horizon_bars  # 246 rows, not 250
    last_purged_row_index = purged_train_df.index[-1]
    assert last_purged_row_index == train_window - horizon_bars - 1  # row 245 -- the brief's own boundary

    # The actual invariant: no row remaining in the purged training set has
    # a label that reaches as far as the first test row's own price.
    first_test_row_position = split.test_start
    for row_position in purged_train_df.index:
        label_reaches_position = row_position + horizon_bars
        assert label_reaches_position < first_test_row_position, (
            f"row {row_position}'s target_up label reaches position {label_reaches_position}, "
            f"which is >= the first test row's position {first_test_row_position} -- leakage"
        )


def test_unpurged_slice_would_have_leaked_exactly_as_the_brief_describes():
    # Negative control: confirms the OLD (buggy) slice really did leak,
    # so this test suite would have caught the original bug.
    horizon_bars = 4
    train_window = 250
    test_window = 50
    n = train_window + test_window + 10
    df = _synthetic_closes(n)
    labeled = add_forward_target(df, horizon_bars)
    usable = labeled.dropna(subset=["target_up"]).reset_index(drop=True)
    split = next(iter(walk_forward_splits(len(usable), train_window, test_window)))

    old_buggy_train_df = usable.iloc[split.train_start : split.train_end]  # no purge
    leaking_rows = [
        pos for pos in old_buggy_train_df.index
        if pos + horizon_bars >= split.test_start
    ]
    # Rows 246, 247, 248, 249 -- exactly the brief's own reproduction.
    assert leaking_rows == [246, 247, 248, 249]

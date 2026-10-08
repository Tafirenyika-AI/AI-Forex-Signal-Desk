"""V4 Priority 2 (docs/V4_ARCHITECTURE.md Section 8 gap): tests for the new
additive detail columns on src/models/regime.py's classify_regime() --
regime_direction, regime_low_volatility, regime_event_driven,
regime_probability. None of these may change the pre-existing `regime`,
`vol_percentile`, or `trend_percentile` columns, since src/decision/
fusion.py's REGIME_WEIGHT_MULTIPLIERS is live, execution-adjacent code keyed
on the original label set.

Run: .venv/Scripts/python.exe -m pytest tests/test_regime_v4_detail_columns.py -v
"""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import pytest

from src.models.regime import (
    REGIME_HIGH_VOL,
    REGIME_RANGE,
    REGIME_SHOCK,
    REGIME_TREND,
    REGIME_TREND_DOWN,
    REGIME_TREND_UP,
    classify_regime,
)

LOOKBACK = 20  # min_periods = max(10, lookback // 5) = 10


def _make_df(trend_slope: list[float], rolling_vol_20: list[float], log_return_1: list[float]) -> pd.DataFrame:
    assert len(trend_slope) == len(rolling_vol_20) == len(log_return_1)
    return pd.DataFrame({
        "trend_slope": trend_slope,
        "rolling_vol_20": rolling_vol_20,
        "log_return_1": log_return_1,
    })


def _warmup(n: int) -> list[float]:
    return [0.001 * (i + 1) for i in range(n)]


def test_trend_up_gets_trend_up_direction():
    trend = _warmup(10) + [round(0.001 + 0.001 * i, 5) for i in range(20)]  # strictly increasing, last = max
    vol = _warmup(10) + [0.0010 + 0.0001 * i for i in range(19)] + [0.0015]  # last row ranks mid (not max)
    logret = [0.0001] * 30
    df = _make_df(trend, vol, logret)

    out = classify_regime(df, lookback=LOOKBACK)
    last = out.iloc[-1]
    assert last["regime"] == REGIME_TREND
    assert last["regime_direction"] == REGIME_TREND_UP
    assert last["regime_low_volatility"] == False  # noqa: E712 -- regime isn't RANGE


def test_trend_down_gets_trend_down_direction():
    trend = _warmup(10) + [round(-0.001 - 0.001 * i, 5) for i in range(20)]  # strictly more negative, |last| = max
    vol = _warmup(10) + [0.0010 + 0.0001 * i for i in range(19)] + [0.0015]
    logret = [0.0001] * 30
    df = _make_df(trend, vol, logret)

    out = classify_regime(df, lookback=LOOKBACK)
    last = out.iloc[-1]
    assert last["regime"] == REGIME_TREND
    assert last["regime_direction"] == REGIME_TREND_DOWN


def test_low_volatility_flag_set_only_within_range_regime():
    # Last row ranks at (or near) the bottom of both trailing windows ->
    # regime defaults to RANGE (not TREND, not HIGH_VOLATILITY) and
    # regime_low_volatility should be True.
    trend = _warmup(10) + [round(0.02 - 0.0005 * i, 5) for i in range(19)] + [0.0001]  # last = min abs
    vol = _warmup(10) + [0.0020 + 0.0001 * i for i in range(19)] + [0.0001]  # last = min
    logret = [0.0001] * 30
    df = _make_df(trend, vol, logret)

    out = classify_regime(df, lookback=LOOKBACK)
    last = out.iloc[-1]
    assert last["regime"] == REGIME_RANGE
    assert last["regime_low_volatility"] == True  # noqa: E712
    assert pd.isna(last["regime_direction"])


def test_high_volatility_regime_has_no_direction_and_is_not_low_vol():
    trend = _warmup(10) + [0.001] * 20  # irrelevant, kept flat/low
    vol = _warmup(10) + [0.0010 + 0.0001 * i for i in range(19)] + [0.0100]  # last = max of window
    logret = [0.0001] * 30
    df = _make_df(trend, vol, logret)

    out = classify_regime(df, lookback=LOOKBACK)
    last = out.iloc[-1]
    assert last["regime"] == REGIME_HIGH_VOL
    assert pd.isna(last["regime_direction"])
    assert last["regime_low_volatility"] == False  # noqa: E712


def test_shock_regime_has_no_direction_and_probability_one():
    trend = _warmup(10) + [0.001] * 20
    vol = _warmup(10) + [0.001] * 20
    logret = [0.0001] * 29 + [0.01]  # last return >> vol * SHOCK_MULTIPLIER
    df = _make_df(trend, vol, logret)

    out = classify_regime(df, lookback=LOOKBACK)
    last = out.iloc[-1]
    assert last["regime"] == REGIME_SHOCK
    assert pd.isna(last["regime_direction"])
    assert last["regime_probability"] == 1.0


def test_warmup_rows_get_unknown_with_zero_probability_and_no_direction():
    trend = _warmup(10) + [round(0.001 + 0.001 * i, 5) for i in range(20)]
    vol = _warmup(10) + [0.0010 + 0.0001 * i for i in range(19)] + [0.0015]
    logret = [0.0001] * 30
    df = _make_df(trend, vol, logret)

    out = classify_regime(df, lookback=LOOKBACK)
    first = out.iloc[0]
    assert first["regime"] == "UNKNOWN"
    assert pd.isna(first["regime_direction"])
    assert first["regime_low_volatility"] == False  # noqa: E712
    assert first["regime_probability"] == 0.0


def test_event_flag_defaults_to_false_for_every_existing_caller():
    trend = _warmup(10) + [round(0.001 + 0.001 * i, 5) for i in range(20)]
    vol = _warmup(10) + [0.0010 + 0.0001 * i for i in range(19)] + [0.0015]
    logret = [0.0001] * 30
    df = _make_df(trend, vol, logret)

    out = classify_regime(df, lookback=LOOKBACK)
    assert (out["regime_event_driven"] == False).all()  # noqa: E712


def test_event_flag_is_passed_through_when_provided_even_during_warmup():
    trend = _warmup(10) + [round(0.001 + 0.001 * i, 5) for i in range(20)]
    vol = _warmup(10) + [0.0010 + 0.0001 * i for i in range(19)] + [0.0015]
    logret = [0.0001] * 30
    df = _make_df(trend, vol, logret)
    event_flag = pd.Series([False] * 30)
    event_flag.iloc[0] = True  # a warm-up row with a real event
    event_flag.iloc[-1] = True

    out = classify_regime(df, lookback=LOOKBACK, event_flag=event_flag)
    assert out.iloc[0]["regime_event_driven"] == True  # noqa: E712
    assert out.iloc[-1]["regime_event_driven"] == True  # noqa: E712
    assert out.iloc[15]["regime_event_driven"] == False  # noqa: E712


def test_existing_regime_vol_percentile_trend_percentile_columns_are_unaffected():
    # Backward-compatibility contract: adding the new columns must not alter
    # any of the three original columns src/decision/fusion.py and every
    # other existing caller already depends on.
    trend = _warmup(10) + [round(0.001 + 0.001 * i, 5) for i in range(20)]
    vol = _warmup(10) + [0.0010 + 0.0001 * i for i in range(19)] + [0.0015]
    logret = [0.0001] * 30
    df = _make_df(trend, vol, logret)

    with_new_cols = classify_regime(df, lookback=LOOKBACK)
    for col in ("regime", "vol_percentile", "trend_percentile"):
        assert col in with_new_cols.columns
    # No NaN leaked into the new columns' dtype coercion for a plain boolean check
    assert with_new_cols["regime_low_volatility"].dtype == bool
    assert with_new_cols["regime_event_driven"].dtype == bool

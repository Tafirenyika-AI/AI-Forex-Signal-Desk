"""Unit tests for Equity V2 Phase 8: src/features/equity_cross_market.py.

Synthetic H1 candle frames, same style as src/features/engine.py's own
tests use synthetic candle data. A separate live check (not a pytest
test) was run against real SPY/sector-ETF/ticker candle data Phase 8's
backfill extension wrote to production; see
docs/EQUITY_V2_IMPLEMENTATION_LOG.md.

Run: .venv/Scripts/python.exe -m pytest tests/test_equity_cross_market.py -v
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import pytest

from src.features.equity_cross_market import approx_vwap, compute_cross_market_features


def _hourly_frame(start: datetime, closes: list[float], volume: float = 1000.0) -> pd.DataFrame:
    """Builds a simple H1 frame: open==prior close (or first close),
    high/low a small band around close, constant volume unless overridden."""
    rows = []
    prev_close = closes[0]
    for i, close in enumerate(closes):
        time = start + timedelta(hours=i)
        open_ = prev_close
        rows.append({
            "time": time, "open": open_, "high": max(open_, close) + 0.1,
            "low": min(open_, close) - 0.1, "close": close, "volume": volume,
        })
        prev_close = close
    return pd.DataFrame(rows)


def test_no_data_at_all_before_as_of_means_everything_missing():
    df = _hourly_frame(datetime(2026, 10, 2, 14, tzinfo=timezone.utc), [100.0])
    features = compute_cross_market_features(df, None, None, "NVDA", datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert features.relative_volume is None
    assert set(features.missing_metrics) == {
        "relative_volume", "gap_pct", "volatility_percentile",
        "return_vs_spy", "return_vs_sector_etf", "vwap_distance",
    }


def test_relative_volume_above_average_detected():
    start = datetime(2026, 10, 1, 14, tzinfo=timezone.utc)
    closes = [100.0] * 25
    df = _hourly_frame(start, closes, volume=1000.0)
    df.loc[df.index[-1], "volume"] = 3000.0  # today's bar: 3x the trailing average
    as_of = df["time"].iloc[-1]
    features = compute_cross_market_features(df, None, None, "NVDA", as_of)
    assert features.relative_volume == pytest.approx(3.0, rel=0.01)


def test_gap_pct_overnight_gap_between_calendar_days():
    day1 = [datetime(2026, 10, 1, h, tzinfo=timezone.utc) for h in (14, 15, 16)]
    day2 = [datetime(2026, 10, 2, h, tzinfo=timezone.utc) for h in (14, 15)]
    rows = [
        {"time": day1[0], "open": 100.0, "high": 101, "low": 99, "close": 100.0, "volume": 1000},
        {"time": day1[1], "open": 100.0, "high": 101, "low": 99, "close": 101.0, "volume": 1000},
        {"time": day1[2], "open": 101.0, "high": 103, "low": 100, "close": 102.0, "volume": 1000},  # prior day's close: 102
        {"time": day2[0], "open": 105.0, "high": 106, "low": 104, "close": 105.5, "volume": 1000},  # today's open: 105 -- a real overnight gap up
        {"time": day2[1], "open": 105.5, "high": 107, "low": 105, "close": 106.0, "volume": 1000},
    ]
    df = pd.DataFrame(rows)
    features = compute_cross_market_features(df, None, None, "NVDA", df["time"].iloc[-1])
    expected_gap = (105.0 - 102.0) / 102.0
    assert features.gap_pct == pytest.approx(expected_gap)


def test_gap_pct_none_with_only_one_calendar_day_of_data():
    df = _hourly_frame(datetime(2026, 10, 2, 10, tzinfo=timezone.utc), [100.0, 101.0, 102.0])
    features = compute_cross_market_features(df, None, None, "NVDA", df["time"].iloc[-1])
    assert features.gap_pct is None
    assert "gap_pct" in features.missing_metrics


def test_volatility_percentile_todays_wide_range_ranks_high():
    start = datetime(2026, 10, 1, 14, tzinfo=timezone.utc)
    rows = []
    for i in range(20):
        rows.append({"time": start + timedelta(hours=i), "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 1000})
    rows[-1]["high"], rows[-1]["low"] = 110.0, 90.0  # today: a much wider range than every prior bar
    df = pd.DataFrame(rows)
    features = compute_cross_market_features(df, None, None, "NVDA", df["time"].iloc[-1])
    assert features.volatility_percentile == pytest.approx(1.0)  # ranks at/above every bar in the window


def test_return_vs_spy_outperformance_detected():
    start = datetime(2026, 10, 1, 14, tzinfo=timezone.utc)
    ticker_closes = [100.0] * 20 + [120.0]  # +20% over the window
    spy_closes = [400.0] * 20 + [404.0]     # +1% over the window
    ticker_df = _hourly_frame(start, ticker_closes)
    spy_df = _hourly_frame(start, spy_closes)
    as_of = ticker_df["time"].iloc[-1]
    features = compute_cross_market_features(ticker_df, spy_df, None, "NVDA", as_of)
    expected = 0.20 - 0.01
    assert features.return_vs_spy == pytest.approx(expected, rel=0.01)


def test_return_vs_spy_none_when_no_spy_data_provided():
    start = datetime(2026, 10, 1, 14, tzinfo=timezone.utc)
    ticker_df = _hourly_frame(start, [100.0] * 25)
    features = compute_cross_market_features(ticker_df, None, None, "NVDA", ticker_df["time"].iloc[-1])
    assert features.return_vs_spy is None
    assert "return_vs_spy" in features.missing_metrics


def test_vwap_distance_price_above_vwap_is_positive():
    start = datetime(2026, 10, 1, 14, tzinfo=timezone.utc)
    closes = [100.0] * 19 + [150.0]  # a sharp spike on the latest bar, well above the trailing VWAP
    df = _hourly_frame(start, closes)
    features = compute_cross_market_features(df, None, None, "NVDA", df["time"].iloc[-1])
    assert features.vwap_distance is not None
    assert features.vwap_distance > 0


def test_approx_vwap_is_volume_weighted_not_simple_average():
    # Two bars: one tiny-volume at a high price, one huge-volume at a low
    # price -- VWAP must land close to the huge-volume bar's price, not
    # the simple average of the two prices.
    df = pd.DataFrame([
        {"time": datetime(2026, 10, 1, 14, tzinfo=timezone.utc), "open": 200, "high": 201, "low": 199, "close": 200.0, "volume": 1.0},
        {"time": datetime(2026, 10, 1, 15, tzinfo=timezone.utc), "open": 100, "high": 101, "low": 99, "close": 100.0, "volume": 1_000_000.0},
    ])
    vwap = approx_vwap(df, window=2)
    assert vwap.iloc[-1] == pytest.approx(100.0, abs=0.5)  # dominated by the huge-volume bar, not 150 (simple average)

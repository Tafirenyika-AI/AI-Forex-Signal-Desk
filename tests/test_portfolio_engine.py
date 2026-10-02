"""Unit tests for Equity V2 Phase 12: src/backtest/portfolio_engine.py.

Synthetic candle frames per ticker, same style as
tests/test_equity_cross_market.py's own fixtures. A separate live check
(not a pytest test) was run against real NVDA/AAPL/MSFT candle data with
a simple deterministic signal rule; see
docs/EQUITY_V2_IMPLEMENTATION_LOG.md.

Run: .venv/Scripts/python.exe -m pytest tests/test_portfolio_engine.py -v
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

from src.backtest.portfolio_engine import Signal, run_portfolio_backtest


def _flat_frame(start: datetime, n: int, close: float) -> pd.DataFrame:
    rows = []
    for i in range(n):
        rows.append({
            "time": start + timedelta(hours=i), "open": close, "high": close + 0.5, "low": close - 0.5,
            "close": close, "volume": 1000,
        })
    return pd.DataFrame(rows)


def test_two_overlapping_signals_that_fit_both_open():
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    candles = {
        "A": _flat_frame(start, 20, 100.0),
        "B": _flat_frame(start, 20, 50.0),
    }
    signals = [
        Signal(ticker="A", entry_time=candles["A"]["time"].iloc[0], entry_idx=0, direction=1,
               stop_distance=2.0, target_distance=4.0, sector="Tech"),
        Signal(ticker="B", entry_time=candles["B"]["time"].iloc[0], entry_idx=0, direction=1,
               stop_distance=1.0, target_distance=2.0, sector="Energy"),
    ]
    # risk_per_trade_pct kept small relative to the stop distance here --
    # with a 2% stop and the default-scale risk, a naive 1% risk_per_trade
    # would size EACH position at ~50% of equity (dollars_at_risk /
    # stop_distance_pct = (100_000*0.01) / 0.02 = $50,000), which is itself
    # a real, useful finding about how sensitive risk-based sizing is to
    # stop tightness (see this module's own docstring) but would make this
    # particular test about sector/capital overlap instead about a single
    # oversized position. 0.002 keeps each position a realistic ~10% of
    # equity so the test exercises what it's actually meant to.
    result = run_portfolio_backtest(signals, candles, starting_capital=100_000.0, risk_per_trade_pct=0.002)
    assert result.signals_skipped_insufficient_capital == 0
    assert result.signals_skipped_sector_concentration == 0
    assert len(result.closed_positions) == 2


def test_signal_skipped_when_insufficient_capital_remains():
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    # Six same-entry-time signals across six different sectors (so the
    # sector cap never triggers), each capped by max_position_pct_of_equity
    # at 20% of equity ($200 of $1000) -- the first 5 consume the entire
    # $1000, so the 6th must be rejected purely for lack of free cash.
    candles = {t: _flat_frame(start, 20, 100.0) for t in "ABCDEF"}
    signals = [
        Signal(ticker=t, entry_time=candles[t]["time"].iloc[0], entry_idx=0, direction=1,
               stop_distance=0.1, target_distance=0.2, sector=f"Sector{t}")
        for t in "ABCDEF"
    ]
    result = run_portfolio_backtest(signals, candles, starting_capital=1000.0, risk_per_trade_pct=0.5, max_position_pct_of_equity=0.20)
    assert result.signals_skipped_insufficient_capital >= 1


def test_sector_concentration_cap_rejects_a_third_same_sector_signal():
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    candles = {t: _flat_frame(start, 20, 100.0) for t in ["A", "B", "C"]}
    signals = [
        Signal(ticker=t, entry_time=candles[t]["time"].iloc[0], entry_idx=0, direction=1,
               stop_distance=5.0, target_distance=10.0, sector="Tech")
        for t in ["A", "B", "C"]
    ]
    result = run_portfolio_backtest(signals, candles, starting_capital=100_000.0, risk_per_trade_pct=0.05, max_sector_exposure_pct=0.10)
    assert result.signals_skipped_sector_concentration >= 1


def test_equity_curve_marks_to_market_mid_trade_not_just_at_entry_exit():
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(10):
        close = 100.0 + i * 2.0  # steadily rising
        rows.append({"time": start + timedelta(hours=i), "open": close, "high": close + 0.5, "low": close - 0.5, "close": close, "volume": 1000})
    candles = {"A": pd.DataFrame(rows)}
    signals = [Signal(ticker="A", entry_time=candles["A"]["time"].iloc[0], entry_idx=0, direction=1,
                       stop_distance=50.0, target_distance=50.0, sector="Tech")]  # wide stop/target -- won't exit early
    result = run_portfolio_backtest(signals, candles, starting_capital=100_000.0, risk_per_trade_pct=0.01)
    # Equity partway through the trade (price has risen) must be HIGHER
    # than at entry -- proof the curve reflects real intermediate prices,
    # not just a flat line between entry and exit.
    mid_point_equity = result.equity_curve.iloc[len(result.equity_curve) // 2]
    assert mid_point_equity > result.starting_capital


def test_still_open_position_at_the_end_is_flushed_and_counted():
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    candles = {"A": _flat_frame(start, 15, 100.0)}
    signals = [Signal(ticker="A", entry_time=candles["A"]["time"].iloc[0], entry_idx=0, direction=1,
                       stop_distance=50.0, target_distance=50.0, sector="Tech")]  # never hits stop/target -- runs to "eod"
    result = run_portfolio_backtest(signals, candles, starting_capital=100_000.0, risk_per_trade_pct=0.01)
    assert len(result.closed_positions) == 1
    assert result.closed_positions[0].exit_reason == "eod"


def test_beta_and_spy_relative_return_computed_against_a_flat_spy():
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(10):
        close = 100.0 + i * 1.0
        rows.append({"time": start + timedelta(hours=i), "open": close, "high": close + 0.3, "low": close - 0.3, "close": close, "volume": 1000})
    candles = {"A": pd.DataFrame(rows)}
    spy_candles = _flat_frame(start, 10, 500.0)  # perfectly flat SPY -- zero variance
    signals = [Signal(ticker="A", entry_time=candles["A"]["time"].iloc[0], entry_idx=0, direction=1,
                       stop_distance=50.0, target_distance=50.0, sector="Tech")]
    result = run_portfolio_backtest(signals, candles, starting_capital=100_000.0, risk_per_trade_pct=0.01, spy_candles=spy_candles)
    # Zero-variance SPY makes beta mathematically undefined (division by
    # zero variance) -- must be honestly None, never a fabricated number.
    assert result.beta_vs_spy is None
    assert result.spy_relative_return_pct == pytest.approx(result.net_return_pct)  # SPY had 0% return


def test_max_position_pct_of_equity_caps_an_oversized_risk_based_allocation():
    # Real finding this guards: a tight stop relative to price (common for
    # a liquid, low-volatility large-cap on an hourly chart, confirmed
    # live against real NVDA data) makes pure risk-based sizing allocate
    # a huge fraction of equity to ONE position. A 0.5% stop with 1% risk
    # implies dollars_at_risk/stop_pct = (100_000*0.01)/0.005 = $200,000 --
    # double the entire account -- without this cap.
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    candles = {"A": _flat_frame(start, 10, 100.0)}
    signals = [Signal(ticker="A", entry_time=candles["A"]["time"].iloc[0], entry_idx=0, direction=1,
                       stop_distance=0.5, target_distance=1.0, sector="Tech")]  # stop_distance_pct = 0.5%
    result = run_portfolio_backtest(
        signals, candles, starting_capital=100_000.0, risk_per_trade_pct=0.01, max_position_pct_of_equity=0.20,
    )
    assert len(result.closed_positions) == 1
    assert result.closed_positions[0].dollars_allocated == pytest.approx(20_000.0)  # capped at 20% of equity, not $200k


def test_no_signals_returns_empty_result_not_crash():
    result = run_portfolio_backtest([], {}, starting_capital=100_000.0)
    assert result.ending_capital == 100_000.0
    assert result.closed_positions == []
    assert len(result.equity_curve) == 0

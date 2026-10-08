"""V4 Priority 6 — src/backtest/performance_metrics.py.

Tests compute_performance_metrics against hand-fabricated
PortfolioBacktestResult objects with known-by-construction correct
answers, not a real simulation run (src/backtest/portfolio_engine.py's own
simulation already has its own tests).

Run: .venv/Scripts/python.exe -m pytest tests/test_performance_metrics.py -v
"""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import pytest

from src.backtest.performance_metrics import compute_performance_metrics
from src.backtest.portfolio_engine import ClosedPosition, PortfolioBacktestResult

T0 = pd.Timestamp("2026-01-01", tz="UTC")


def _position(pnl_dollars, dollars_allocated=10000.0, day=0):
    return ClosedPosition(
        ticker="TEST", sector="Technology", entry_time=T0 + pd.Timedelta(days=day),
        exit_time=T0 + pd.Timedelta(days=day + 1), direction=1, entry_price=100.0,
        exit_price=100.0 + pnl_dollars / dollars_allocated * 100.0, dollars_allocated=dollars_allocated,
        pnl_dollars=pnl_dollars, exit_reason="target",
    )


def _result(positions, equity_values, starting_capital=100000.0):
    equity_curve = pd.Series(
        equity_values, index=pd.date_range(T0, periods=len(equity_values), freq="D", tz="UTC"),
    )
    return PortfolioBacktestResult(
        starting_capital=starting_capital, ending_capital=starting_capital + sum(p.pnl_dollars for p in positions),
        net_return_pct=0.0, max_drawdown_pct=0.0, equity_curve=equity_curve, closed_positions=positions,
        signals_skipped_insufficient_capital=0, signals_skipped_sector_concentration=0,
        sector_exposure_timeline=pd.DataFrame(), beta_vs_spy=None, spy_relative_return_pct=None,
    )


def test_no_trades_returns_all_none_honestly():
    result = _result([], [100000.0])
    metrics = compute_performance_metrics(result)
    assert metrics.n_trades == 0
    assert metrics.win_rate is None and metrics.profit_factor is None


def test_win_rate_and_profit_factor_computed_correctly():
    positions = [_position(1000, day=0), _position(1000, day=1), _position(-500, day=2), _position(-500, day=3)]
    equity_values = [100000.0] * 10
    result = _result(positions, equity_values)
    metrics = compute_performance_metrics(result)
    assert metrics.n_trades == 4
    assert metrics.win_rate == pytest.approx(0.5)
    assert metrics.profit_factor == pytest.approx(2000 / 1000)


def test_all_wins_gives_infinite_profit_factor_not_a_crash():
    positions = [_position(500, day=0), _position(300, day=1)]
    result = _result(positions, [100000.0] * 5)
    metrics = compute_performance_metrics(result)
    assert metrics.profit_factor == float("inf")


def test_trade_expectancy_is_mean_pnl_over_dollars_allocated():
    positions = [_position(1000, dollars_allocated=10000, day=0), _position(-500, dollars_allocated=10000, day=1)]
    result = _result(positions, [100000.0] * 5)
    metrics = compute_performance_metrics(result)
    assert metrics.trade_expectancy_pct == pytest.approx((0.10 + -0.05) / 2 * 100)


def test_turnover_is_total_dollars_traded_over_starting_capital():
    positions = [_position(0, dollars_allocated=20000, day=0), _position(0, dollars_allocated=30000, day=1)]
    result = _result(positions, [100000.0] * 5, starting_capital=100000.0)
    metrics = compute_performance_metrics(result)
    assert metrics.turnover == pytest.approx(50000 / 100000)


def test_sharpe_is_positive_for_a_steadily_rising_equity_curve():
    positions = [_position(1000, day=0)]
    equity_values = [100000 * (1.001 ** i) for i in range(60)]  # steady daily gains, zero variance-free risk
    result = _result(positions, equity_values)
    metrics = compute_performance_metrics(result)
    assert metrics.sharpe_ratio is not None and metrics.sharpe_ratio > 0


def test_sortino_ignores_upside_volatility_unlike_sharpe():
    positions = [_position(1000, day=0)]
    # Big upside swings, small consistent downside -- Sortino (downside-only
    # deviation) should come out higher than Sharpe (full deviation) here.
    equity_values = [100000.0]
    for i in range(1, 60):
        move = 0.05 if i % 3 == 0 else -0.001
        equity_values.append(equity_values[-1] * (1 + move))
    result = _result(positions, equity_values)
    metrics = compute_performance_metrics(result)
    assert metrics.sharpe_ratio is not None and metrics.sortino_ratio is not None
    assert metrics.sortino_ratio > metrics.sharpe_ratio

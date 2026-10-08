"""V4 Priority 6 (brief Section 14 gap, docs/V4_ARCHITECTURE.md Priority 6):
named Sharpe/Sortino/profit-factor/win-rate/turnover/expectancy fields for
`src/backtest/portfolio_engine.py`'s `PortfolioBacktestResult`.

Pure post-processing over an already-produced result — deliberately NOT
added as new fields on `PortfolioBacktestResult` itself or computed inside
`run_portfolio_backtest()`, to avoid touching that already-tested core
simulation. `docs/V4_ARCHITECTURE.md`'s own Priority 6 note: "some exist
under different names" already (e.g. `net_return_pct`, `max_drawdown_pct`)
— this module adds the ones that are genuinely missing, not duplicates.

Sharpe/Sortino annualization uses an EMPIRICAL periods-per-year derived
from the equity curve's own real timestamps, not a hardcoded 252 (which
assumes daily bars) — `run_portfolio_backtest`'s equity curve is an
irregular union of whatever candle granularity the input signals actually
used (H1/H4/D), so a fixed calendar constant would silently misrepresent
the real annualized figure.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from src.backtest.portfolio_engine import PortfolioBacktestResult


@dataclass(frozen=True)
class PerformanceMetrics:
    n_trades: int
    win_rate: float | None
    profit_factor: float | None  # gross wins / gross losses (abs); None if no losses AND no wins, inf if losses are zero but wins exist
    trade_expectancy_pct: float | None  # mean(pnl_dollars / dollars_allocated) across closed positions
    turnover: float | None  # total dollars_allocated traded / starting_capital
    sharpe_ratio: float | None
    sortino_ratio: float | None


def compute_performance_metrics(result: PortfolioBacktestResult) -> PerformanceMetrics:
    n_trades = len(result.closed_positions)
    if n_trades == 0:
        return PerformanceMetrics(0, None, None, None, None, None, None)

    wins = [p.pnl_dollars for p in result.closed_positions if p.pnl_dollars > 0]
    losses = [p.pnl_dollars for p in result.closed_positions if p.pnl_dollars < 0]
    win_rate = len(wins) / n_trades

    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    if gross_loss > 0:
        profit_factor = gross_win / gross_loss
    elif gross_win > 0:
        profit_factor = float("inf")  # every trade won -- a real, if unusual, result; not fabricated
    else:
        profit_factor = None  # no trades had any P&L at all (every position broke exactly even)

    sizeable = [p for p in result.closed_positions if p.dollars_allocated]
    trade_expectancy_pct = (
        float(np.mean([p.pnl_dollars / p.dollars_allocated for p in sizeable]) * 100) if sizeable else None
    )

    turnover = (
        sum(p.dollars_allocated for p in result.closed_positions) / result.starting_capital
        if result.starting_capital else None
    )

    sharpe_ratio = None
    sortino_ratio = None
    equity_curve = result.equity_curve
    if len(equity_curve) > 2:
        returns = equity_curve.pct_change().dropna()
        if len(returns) > 1 and returns.std() > 0:
            span_days = (equity_curve.index[-1] - equity_curve.index[0]).total_seconds() / 86400
            periods_per_year = (len(equity_curve) - 1) / span_days * 365.25 if span_days > 0 else None
            if periods_per_year:
                sharpe_ratio = float(returns.mean() / returns.std() * np.sqrt(periods_per_year))
                downside = returns[returns < 0]
                if len(downside) > 1 and downside.std() > 0:
                    sortino_ratio = float(returns.mean() / downside.std() * np.sqrt(periods_per_year))

    return PerformanceMetrics(
        n_trades=n_trades, win_rate=win_rate, profit_factor=profit_factor,
        trade_expectancy_pct=trade_expectancy_pct, turnover=turnover,
        sharpe_ratio=sharpe_ratio, sortino_ratio=sortino_ratio,
    )

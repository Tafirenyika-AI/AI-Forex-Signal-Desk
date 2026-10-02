"""Equity V2 Phase 12 — portfolio-level backtester.

Fixes src/backtest/engine.py's own disclosed limitation (Phase 0 audit,
section 11): that engine simulates ONE position at a time, with no
concept of a shared capital pool across simultaneously open positions in
different tickers — a correlated cluster of signals (e.g. three
Technology names all firing the same week) backtested in isolation per
ticker, as every prior backtest in this project has done, silently
assumes each trade had access to the FULL account. This module instead
tracks one real, finite capital pool across every given ticker's signals,
reserves capital for every open position, and refuses a new entry when
not enough is actually free — then builds the REAL combined equity curve
that capital contention produces, not an optimistic per-ticker fiction.

Reuses src/backtest/engine.py's own _simulate_exit (the same ATR-sized-
once-at-entry stop/target walk, same gap-aware stop fill) rather than
re-deriving exit mechanics — this module's own job is capital allocation
and multi-position bookkeeping across tickers, not stop/target
simulation, which already exists and is already tested.

Signal generation (which ticker, which direction, what stop/target
distance) is explicitly NOT this module's job — that's Phase 10/11's
(challenger models) and src/decision/fusion.py's (ATR-sized stops) via
whatever upstream process the caller used. This module accepts already-
decided signals as input.

Design simplification, deliberate: because a position's own exit (given
its entry/stop/target) is deterministic and independent of what else is
happening in the portfolio (real brokers don't move one stock's price
because another position closed), each signal's full lifetime (exit
time/price/reason) is resolved once, up front, via _simulate_exit —
there is no need for a bar-by-bar shared clock to determine THAT. A
separate mark-to-market pass then walks a unified timeline (the union of
every ticker's own bar times) to build the actual equity curve, since
"what was total portfolio equity worth at any given moment" does need
every ticker's current price, not just entry/exit prices.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from src.backtest.engine import _simulate_exit


@dataclass(frozen=True)
class Signal:
    ticker: str
    entry_time: pd.Timestamp
    entry_idx: int  # this ticker's own candles_df row position at entry_time
    direction: int  # 1 long, -1 short
    stop_distance: float
    target_distance: float
    sector: str | None = None


@dataclass(frozen=True)
class ClosedPosition:
    ticker: str
    sector: str | None
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    direction: int
    entry_price: float
    exit_price: float
    dollars_allocated: float
    pnl_dollars: float
    exit_reason: str


@dataclass(frozen=True)
class PortfolioBacktestResult:
    starting_capital: float
    ending_capital: float
    net_return_pct: float
    max_drawdown_pct: float
    equity_curve: pd.Series
    closed_positions: list[ClosedPosition]
    signals_skipped_insufficient_capital: int
    signals_skipped_sector_concentration: int
    sector_exposure_timeline: pd.DataFrame  # index=time, columns=sector, values=% of equity at that time
    beta_vs_spy: float | None
    spy_relative_return_pct: float | None


def _resolve_full_lifetime(signal: Signal, candles_df: pd.DataFrame) -> tuple[pd.Timestamp, float, float, str]:
    """Runs this signal's entry through to its eventual exit, using the
    existing, already-tested single-position exit walker. Returns
    (exit_time, entry_price, exit_price, exit_reason)."""
    entry_price = float(candles_df.iloc[signal.entry_idx]["close"])
    exit_idx, exit_price, exit_reason = _simulate_exit(
        candles_df, signal.entry_idx, signal.direction, entry_price, signal.stop_distance, signal.target_distance,
    )
    exit_time = candles_df.iloc[exit_idx]["time"]
    return exit_time, entry_price, exit_price, exit_reason


def _position_size(
    current_equity: float, entry_price: float, stop_distance: float,
    risk_per_trade_pct: float, max_position_pct_of_equity: float,
) -> float:
    """Risk-based sizing: the dollar amount allocated is whatever makes a
    stop-out lose exactly risk_per_trade_pct of current equity — same
    convention this project's live risk governor already sizes by (a
    position's $-at-risk, not its raw notional, is the thing capped) —
    capped at max_position_pct_of_equity of current equity regardless.

    That cap is not decoration: confirmed live against real NVDA data
    (2026-10-02) that pure risk-based sizing, with no notional ceiling,
    is genuinely dangerous for a liquid, lower-volatility large-cap stock.
    NVDA's real H1 ATR was ~1.3% of price, so a 1%-of-equity risk target
    with a 1.5x-ATR stop implied allocating ~78% of total equity to ONE
    position — mathematically correct given the risk-based formula alone
    (a tighter stop relative to price always inflates the implied size to
    hold dollar-risk constant), but not a realistic simulation of how any
    real portfolio should ever actually be sized. This is exactly the
    gap Phase 13's risk governor extensions are meant to address more
    fully (correlation-aware sizing, drawdown circuit breakers); this cap
    is the minimum sane guard a backtester needs even before that lands."""
    stop_distance_pct = stop_distance / entry_price
    if stop_distance_pct <= 0:
        return 0.0
    dollars_at_risk = current_equity * risk_per_trade_pct
    risk_based_size = dollars_at_risk / stop_distance_pct
    return min(risk_based_size, current_equity * max_position_pct_of_equity)


def _price_at_or_before(candles_df: pd.DataFrame, t: pd.Timestamp) -> float | None:
    eligible = candles_df[candles_df["time"] <= t]
    return float(eligible.iloc[-1]["close"]) if not eligible.empty else None


def run_portfolio_backtest(
    signals: list[Signal],
    candles_by_ticker: dict[str, pd.DataFrame],
    starting_capital: float = 100_000.0,
    risk_per_trade_pct: float = 0.01,
    max_position_pct_of_equity: float = 0.20,
    max_sector_exposure_pct: float = 0.40,
    spy_candles: pd.DataFrame | None = None,
) -> PortfolioBacktestResult:
    ordered_signals = sorted(signals, key=lambda s: s.entry_time)

    cash = starting_capital
    open_positions: list[dict] = []  # each: ticker, sector, entry_time, exit_time, direction, entry_price, dollars_allocated
    closed_positions: list[ClosedPosition] = []
    skipped_capital = 0
    skipped_sector = 0

    def _release_matured(up_to_time: pd.Timestamp) -> None:
        nonlocal cash
        still_open = []
        for pos in open_positions:
            if pos["exit_time"] <= up_to_time:
                pnl_pct = pos["direction"] * (pos["exit_price"] - pos["entry_price"]) / pos["entry_price"]
                pnl_dollars = pos["dollars_allocated"] * pnl_pct
                cash += pos["dollars_allocated"] + pnl_dollars
                closed_positions.append(ClosedPosition(
                    ticker=pos["ticker"], sector=pos["sector"], entry_time=pos["entry_time"], exit_time=pos["exit_time"],
                    direction=pos["direction"], entry_price=pos["entry_price"], exit_price=pos["exit_price"],
                    dollars_allocated=pos["dollars_allocated"], pnl_dollars=pnl_dollars, exit_reason=pos["exit_reason"],
                ))
            else:
                still_open.append(pos)
        open_positions[:] = still_open

    for signal in ordered_signals:
        _release_matured(signal.entry_time)
        current_equity = cash + sum(p["dollars_allocated"] for p in open_positions)

        candles_df = candles_by_ticker[signal.ticker]
        exit_time, entry_price, exit_price, exit_reason = _resolve_full_lifetime(signal, candles_df)
        dollars_allocated = _position_size(
            current_equity, entry_price, signal.stop_distance, risk_per_trade_pct, max_position_pct_of_equity,
        )
        if dollars_allocated <= 0 or dollars_allocated > cash:
            skipped_capital += 1
            continue

        if signal.sector is not None:
            sector_exposure = sum(p["dollars_allocated"] for p in open_positions if p["sector"] == signal.sector)
            if (sector_exposure + dollars_allocated) / current_equity > max_sector_exposure_pct:
                skipped_sector += 1
                continue

        cash -= dollars_allocated
        open_positions.append({
            "ticker": signal.ticker, "sector": signal.sector, "entry_time": signal.entry_time, "exit_time": exit_time,
            "direction": signal.direction, "entry_price": entry_price, "exit_price": exit_price,
            "dollars_allocated": dollars_allocated, "exit_reason": exit_reason,
        })

    # Flush every position still open after the last signal -- release them
    # all so closed_positions/cash correctly reflect every trade actually
    # taken, not just ones that happened to be followed by a later signal.
    if open_positions:
        last_exit = max(p["exit_time"] for p in open_positions)
        _release_matured(last_exit)

    all_positions = closed_positions  # every position is now closed after the flush above

    # Unified mark-to-market timeline: the union of every ticker's own bar
    # times that fall within the overall signal window, so the equity
    # curve reflects real price movement, not just discrete trade events.
    if all_positions:
        window_start = min(p.entry_time for p in all_positions)
        window_end = max(p.exit_time for p in all_positions)
        timeline = sorted({
            t for df in candles_by_ticker.values()
            for t in df.loc[(df["time"] >= window_start) & (df["time"] <= window_end), "time"]
        })
    else:
        timeline = []

    equity_values = []
    sector_rows = []
    for t in timeline:
        total = starting_capital
        sector_dollars: dict[str, float] = {}
        for pos in all_positions:
            if pos.entry_time > t:
                continue
            if pos.exit_time <= t:
                total += pos.pnl_dollars
            else:
                price_now = _price_at_or_before(candles_by_ticker[pos.ticker], t)
                if price_now is None:
                    continue
                unrealized_pct = pos.direction * (price_now - pos.entry_price) / pos.entry_price
                unrealized = pos.dollars_allocated * unrealized_pct
                total += unrealized
                if pos.sector is not None:
                    sector_dollars[pos.sector] = sector_dollars.get(pos.sector, 0.0) + pos.dollars_allocated + unrealized
        equity_values.append(total)
        sector_rows.append({sector: dollars / total for sector, dollars in sector_dollars.items()} if total else {})

    equity_curve = pd.Series(equity_values, index=pd.Index(timeline, name="time"))
    sector_exposure_timeline = pd.DataFrame(sector_rows, index=pd.Index(timeline, name="time")).fillna(0.0)

    ending_capital = cash + sum(p["dollars_allocated"] for p in open_positions)  # should equal cash after the flush above
    net_return_pct = float((ending_capital - starting_capital) / starting_capital * 100)

    if len(equity_curve) > 0:
        running_max = equity_curve.cummax()
        drawdown = (equity_curve - running_max) / running_max
        max_drawdown_pct = float(drawdown.min() * 100)
    else:
        max_drawdown_pct = float("nan")

    beta_vs_spy = None
    spy_relative_return_pct = None
    if spy_candles is not None and len(equity_curve) > 1:
        spy_prices = pd.Series(
            [_price_at_or_before(spy_candles, t) for t in equity_curve.index], index=equity_curve.index,
        )
        if spy_prices.notna().all():
            portfolio_returns = equity_curve.pct_change().dropna()
            spy_returns = spy_prices.pct_change().dropna()
            aligned = pd.concat([portfolio_returns, spy_returns], axis=1, keys=["portfolio", "spy"]).dropna()
            if len(aligned) > 1 and aligned["spy"].var() > 0:
                beta_vs_spy = float(aligned["portfolio"].cov(aligned["spy"]) / aligned["spy"].var())
            spy_total_return_pct = float((spy_prices.iloc[-1] - spy_prices.iloc[0]) / spy_prices.iloc[0] * 100)
            spy_relative_return_pct = net_return_pct - spy_total_return_pct

    return PortfolioBacktestResult(
        starting_capital=starting_capital, ending_capital=ending_capital, net_return_pct=net_return_pct,
        max_drawdown_pct=max_drawdown_pct, equity_curve=equity_curve, closed_positions=all_positions,
        signals_skipped_insufficient_capital=skipped_capital, signals_skipped_sector_concentration=skipped_sector,
        sector_exposure_timeline=sector_exposure_timeline, beta_vs_spy=beta_vs_spy,
        spy_relative_return_pct=spy_relative_return_pct,
    )

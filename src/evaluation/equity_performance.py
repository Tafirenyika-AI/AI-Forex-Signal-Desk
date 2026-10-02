"""Equity V2 Phase 15 — performance forensics.

Cleanly separates three numbers this project's own history has
repeatedly needed to keep apart (Phase 1's reconciliation work first
established BROKER-VERIFIED vs INTERNAL-CALCULATED P&L as genuinely
different quantities, never to be presented as if they were the same
thing — this phase extends that same discipline to full performance
reporting):

1. BROKER ACCOUNT RETURN — ground truth, straight from Alpaca's own
   portfolio-history endpoint (src/broker/alpaca.py's new
   portfolio_history(), verified live 2026-10-02). Never reconstructed
   from this project's own trade records, which could themselves be
   incomplete or buggy (as several earlier phases this session found).
2. MODEL-ATTRIBUTABLE RETURN — sum of realized_pl_usd from
   trade_outcomes rows WITH a resolved trade_intent_id (this project's
   own signal genuinely caused this trade) over the window. Rows with NO
   resolved trade_intent_id are real, unexplained broker activity — the
   same category Phase 1's reconciliation already flags as
   "unexplained_broker_order" — reported SEPARATELY, never silently
   folded into "the model's" return.
3. CURRENT UNREALIZED P&L — mark-to-market on currently open positions,
   from the broker's own live positions() call. Explicitly disclosed as
   "not yet real" (could reverse before any exit), never combined with
   realized figures as if it were already locked in.

Plus SPY/QQQ benchmark comparison, using the same simple
(end_close - start_close) / start_close convention
src/backtest/portfolio_engine.py's own spy_relative_return_pct already
uses, applied here to this project's own backfilled SPY/QQQ candle
history (Phase 8).

Never combines these ambiguously (the brief's own explicit Phase 15
instruction): PerformanceReport keeps every number as its own named
field, never summed into one blended "performance" figure a reader
might mistake for broker-verified truth.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.data.db import trade_outcomes as trade_outcomes_table


@dataclass(frozen=True)
class PerformanceReport:
    period_start: datetime
    period_end: datetime
    broker_account_return_usd: float | None
    broker_account_return_pct: float | None
    model_attributable_realized_pl_usd: float
    n_model_attributable_trades: int
    unexplained_realized_pl_usd: float
    n_unexplained_trades: int
    current_unrealized_pl_usd: float
    spy_return_pct: float | None
    qqq_return_pct: float | None
    # Named "account_return_vs_..." not "model_return_vs_..." deliberately:
    # this compares the BROKER account return (the only figure with a
    # clean, broker-verified %-of-starting-equity basis) against the
    # benchmark -- NOT the model-attributable dollar P&L alone, which has
    # no consistent capital base to express as a percentage (trade sizes
    # vary). Conflating the two under a "model" label would be exactly
    # the ambiguous combination this phase's own instruction forbids.
    account_return_vs_spy_pct: float | None
    account_return_vs_qqq_pct: float | None


def parse_broker_account_return(
    portfolio_history: dict, period_start: datetime, period_end: datetime,
) -> tuple[float | None, float | None]:
    """(return_usd, return_pct) over [period_start, period_end] from
    Alpaca's own real portfolio-history response (parallel arrays:
    unix-second `timestamp`, `equity`). None, None if the response
    doesn't actually cover this window — never estimated from a partial
    or misaligned range."""
    timestamps = portfolio_history.get("timestamp") or []
    equity = portfolio_history.get("equity") or []
    if not timestamps or not equity or len(timestamps) != len(equity):
        return None, None

    points = [
        (datetime.fromtimestamp(ts, tz=timezone.utc), eq)
        for ts, eq in zip(timestamps, equity) if eq is not None
    ]
    in_window = [(t, eq) for t, eq in points if period_start <= t <= period_end]
    if len(in_window) < 2:
        return None, None

    start_equity = in_window[0][1]
    end_equity = in_window[-1][1]
    if start_equity == 0:
        return None, None
    return_usd = end_equity - start_equity
    return_pct = return_usd / start_equity
    return return_usd, return_pct


def compute_model_attributable_return(
    engine: Engine, user_id: int, broker: str, period_start: datetime, period_end: datetime,
) -> tuple[float, int, float, int]:
    """(model_attributable_pl, n_model_trades, unexplained_pl, n_unexplained_trades)
    -- split purely on whether trade_intent_id resolved, never guessed."""
    stmt = select(trade_outcomes_table).where(
        trade_outcomes_table.c.user_id == user_id,
        trade_outcomes_table.c.broker == broker,
        trade_outcomes_table.c.closed_at >= period_start,
        trade_outcomes_table.c.closed_at <= period_end,
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).fetchall()

    model_pl, unexplained_pl = 0.0, 0.0
    n_model, n_unexplained = 0, 0
    for row in rows:
        if row.trade_intent_id is not None:
            model_pl += row.realized_pl_usd
            n_model += 1
        else:
            unexplained_pl += row.realized_pl_usd
            n_unexplained += 1
    return model_pl, n_model, unexplained_pl, n_unexplained


def compute_current_unrealized_pl(positions_raw: list[dict]) -> float:
    return sum(float(p.get("unrealized_pl", 0.0) or 0.0) for p in positions_raw)


def compute_benchmark_return_pct(candles_df: pd.DataFrame, period_start: datetime, period_end: datetime) -> float | None:
    """Same simple convention src/backtest/portfolio_engine.py's own
    spy_relative_return_pct already uses. None (not estimated) if the
    benchmark's own candle history doesn't actually cover this window --
    confirmed by Phase 12's own live finding that SPY's backfilled
    history can start later than a ticker's."""
    if candles_df.empty:
        return None
    in_window = candles_df[(candles_df["time"] >= period_start) & (candles_df["time"] <= period_end)]
    if len(in_window) < 2:
        return None
    start_close = in_window["close"].iloc[0]
    end_close = in_window["close"].iloc[-1]
    if start_close == 0:
        return None
    return float((end_close - start_close) / start_close)


def build_performance_report(
    engine: Engine, user_id: int, broker: str, period_start: datetime, period_end: datetime,
    portfolio_history: dict, positions_raw: list[dict],
    spy_candles: pd.DataFrame | None = None, qqq_candles: pd.DataFrame | None = None,
) -> PerformanceReport:
    broker_return_usd, broker_return_pct = parse_broker_account_return(portfolio_history, period_start, period_end)
    model_pl, n_model, unexplained_pl, n_unexplained = compute_model_attributable_return(
        engine, user_id, broker, period_start, period_end,
    )
    unrealized_pl = compute_current_unrealized_pl(positions_raw)

    spy_return = compute_benchmark_return_pct(spy_candles, period_start, period_end) if spy_candles is not None else None
    qqq_return = compute_benchmark_return_pct(qqq_candles, period_start, period_end) if qqq_candles is not None else None

    account_vs_spy = (broker_return_pct - spy_return) if broker_return_pct is not None and spy_return is not None else None
    account_vs_qqq = (broker_return_pct - qqq_return) if broker_return_pct is not None and qqq_return is not None else None

    return PerformanceReport(
        period_start=period_start, period_end=period_end,
        broker_account_return_usd=broker_return_usd, broker_account_return_pct=broker_return_pct,
        model_attributable_realized_pl_usd=model_pl, n_model_attributable_trades=n_model,
        unexplained_realized_pl_usd=unexplained_pl, n_unexplained_trades=n_unexplained,
        current_unrealized_pl_usd=unrealized_pl,
        spy_return_pct=spy_return, qqq_return_pct=qqq_return,
        account_return_vs_spy_pct=account_vs_spy, account_return_vs_qqq_pct=account_vs_qqq,
    )

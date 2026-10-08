"""Strategy I — Statistical Pairs Trading (src/strategies/registry.py's "I").

A direct, honest statistical test of the strategy's own hypothesis (Gatev,
Goetzmann & Rouwenhorst 2006): is a candidate pair's price spread
genuinely cointegrated (not just correlated — a different, stronger
claim), and does a statistically stretched spread predict reversion
toward its own historical mean?

Genuinely new statistical infrastructure for this project, per the
registry's own disclosed gap — no cointegration-testing code existed
anywhere in this codebase before this module. Uses `statsmodels`
(newly added to requirements.txt) for a real Augmented Engle-Granger
cointegration test, not a hand-rolled approximation — ADF critical values
are a well-established, non-trivial statistical result this project should
reuse from a standard library, not re-derive.

Pure hypothesis test only — no transaction costs, no sizing, no dollar-
neutral position construction.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.engine import Engine
from statsmodels.tsa.stattools import coint

from src.data.db import candles as candles_table


@dataclass(frozen=True)
class CointegrationResult:
    cointegration_p_value: float | None
    hedge_ratio: float | None  # a ~= hedge_ratio * b + intercept, in log-price terms


@dataclass(frozen=True)
class PairsTradingResult:
    instrument_a: str
    instrument_b: str
    cointegration: CointegrationResult
    n: int
    hit_rate: float | None
    z_score: float | None
    mean_move_in_favor: float | None  # mean(direction * forward spread change), in log-price units


def _load_closes(engine: Engine, broker: str, instrument: str, granularity: str) -> pd.Series | None:
    with engine.connect() as conn:
        rows = conn.execute(
            select(candles_table.c.time, candles_table.c.close)
            .where(
                candles_table.c.broker == broker, candles_table.c.instrument == instrument,
                candles_table.c.granularity == granularity, candles_table.c.complete == True,  # noqa: E712
            )
            .order_by(candles_table.c.time)
        ).all()
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["time", "close"]).drop_duplicates(subset="time").set_index("time")
    return df["close"]


def evaluate_cointegration(log_price_a: pd.Series, log_price_b: pd.Series) -> CointegrationResult:
    """A real Augmented Engle-Granger test — null hypothesis is NO
    cointegration, so a LOW p-value is the evidence this strategy needs."""
    if len(log_price_a) < 20:
        return CointegrationResult(None, None)
    _, p_value, _ = coint(log_price_a, log_price_b, trend="c", autolag="aic")
    hedge_ratio = float(np.polyfit(log_price_b, log_price_a, 1)[0])
    return CointegrationResult(float(p_value), hedge_ratio)


def _evaluate_spread_core(
    spread: pd.Series, spread_lookback: int, entry_z_threshold: float, holding_bars: int,
) -> tuple[int, float | None, float | None, float | None]:
    rolling_mean = spread.rolling(spread_lookback, min_periods=spread_lookback).mean()
    rolling_std = spread.rolling(spread_lookback, min_periods=spread_lookback).std()
    z = (spread - rolling_mean) / rolling_std.replace(0, np.nan)

    direction = -np.sign(z.to_numpy())
    spread_arr = spread.to_numpy()
    n_rows = len(spread)
    forward_change = np.full(n_rows, np.nan)
    valid_forward = np.arange(n_rows) < (n_rows - holding_bars)
    forward_change[valid_forward] = spread_arr[np.arange(n_rows)[valid_forward] + holding_bars] - spread_arr[valid_forward]

    stretched = (z.abs() > entry_z_threshold).to_numpy()
    eligible = stretched & ~np.isnan(forward_change) & (direction != 0) & ~np.isnan(direction)
    n = int(eligible.sum())
    if n == 0:
        return 0, None, None, None

    ev_direction = direction[eligible]
    ev_forward = forward_change[eligible]
    hit = (ev_direction * ev_forward) > 0
    hit_rate = float(hit.mean())
    se = math.sqrt(0.25 / n)
    z_score = (hit_rate - 0.5) / se
    mean_move_in_favor = float((ev_direction * ev_forward).mean())
    return n, hit_rate, z_score, mean_move_in_favor


def evaluate_pairs_trading_hypothesis(
    engine: Engine, broker: str, instrument_a: str, instrument_b: str, granularity: str,
    spread_lookback: int = 60, entry_z_threshold: float = 2.0, holding_bars: int = 10,
) -> PairsTradingResult:
    close_a = _load_closes(engine, broker, instrument_a, granularity)
    close_b = _load_closes(engine, broker, instrument_b, granularity)
    if close_a is None or close_b is None:
        return PairsTradingResult(instrument_a, instrument_b, CointegrationResult(None, None), 0, None, None, None)

    common_index = close_a.index.intersection(close_b.index).sort_values()
    if len(common_index) < spread_lookback + holding_bars + 20:
        return PairsTradingResult(instrument_a, instrument_b, CointegrationResult(None, None), 0, None, None, None)

    log_a = np.log(close_a.reindex(common_index))
    log_b = np.log(close_b.reindex(common_index))
    cointegration = evaluate_cointegration(log_a, log_b)
    if cointegration.hedge_ratio is None:
        return PairsTradingResult(instrument_a, instrument_b, cointegration, 0, None, None, None)

    spread = log_a - cointegration.hedge_ratio * log_b
    n, hit_rate, z_score, mean_move_in_favor = _evaluate_spread_core(
        spread, spread_lookback, entry_z_threshold, holding_bars,
    )
    return PairsTradingResult(instrument_a, instrument_b, cointegration, n, hit_rate, z_score, mean_move_in_favor)

"""Strategy C — Trend Following (src/strategies/registry.py's "C").

A direct, honest statistical test of the strategy's own hypothesis: does
entering on a FRESH transition into TREND (not merely "currently in
TREND" — the registry spec's own distinction, to avoid entering late into
an already-extended move) and holding in `regime_direction`'s direction
(TREND_UP/TREND_DOWN, from V4 Priority 2) produce a forward return that
persists more often than chance? Reuses compute_features()/classify_regime()
wholesale, same as Strategy A/F — this strategy's own registry spec notes
its real exit mechanism (the ATR-ratcheting trailing stop,
src/execution/trailing_stop.py) already exists from earlier work; this
module tests only the ENTRY hypothesis (fixed-horizon forward return), not
a full trailing-stop simulation — that full simulation is explicitly a
later BACKTESTED-stage step, not done here.

Pure hypothesis test only (normal-approximation z-test), same
HYPOTHESIS_TESTED scope as Strategy A/F.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.data.db import candles as candles_table
from src.features.engine import compute_features
from src.models.regime import REGIME_TREND, REGIME_TREND_UP, classify_regime


@dataclass(frozen=True)
class TrendFollowingResult:
    instrument: str
    holding_bars: int
    n: int
    hit_rate: float | None
    z_score: float | None
    mean_move_in_favor: float | None


def _load_candles(engine: Engine, broker: str, instrument: str, granularity: str) -> pd.DataFrame:
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                candles_table.c.time, candles_table.c.open, candles_table.c.high,
                candles_table.c.low, candles_table.c.close,
            )
            .where(
                candles_table.c.broker == broker,
                candles_table.c.instrument == instrument,
                candles_table.c.granularity == granularity,
                candles_table.c.complete == True,  # noqa: E712
            )
            .order_by(candles_table.c.time)
        ).all()
    return pd.DataFrame(rows, columns=["time", "open", "high", "low", "close"]).drop_duplicates(subset="time")


def _evaluate_core(classified: pd.DataFrame, holding_bars: int) -> tuple[int, float | None, float | None, float | None]:
    """classified must already have regime/log_return_1/close columns
    (classify_regime()'s own output shape, plus `regime_direction` for rows
    where regime == TREND)."""
    is_trend = (classified["regime"] == REGIME_TREND).to_numpy()
    was_trend_prev = np.concatenate([[False], is_trend[:-1]])
    fresh_transition = is_trend & ~was_trend_prev

    direction = np.where(classified["regime_direction"].to_numpy() == REGIME_TREND_UP, 1.0, -1.0)

    closes = classified["close"].to_numpy()
    log_close = np.log(closes)
    n_rows = len(classified)
    forward_log_return = np.full(n_rows, np.nan)
    valid_forward = np.arange(n_rows) < (n_rows - holding_bars)
    forward_log_return[valid_forward] = log_close[np.arange(n_rows)[valid_forward] + holding_bars] - log_close[valid_forward]

    eligible = fresh_transition & ~np.isnan(forward_log_return)
    n = int(eligible.sum())
    if n == 0:
        return 0, None, None, None

    ev_direction = direction[eligible]
    ev_forward = forward_log_return[eligible]
    hit = (ev_direction * ev_forward) > 0
    hit_rate = float(hit.mean())
    se = math.sqrt(0.25 / n)
    z_score = (hit_rate - 0.5) / se
    mean_move_in_favor = float((ev_direction * ev_forward).mean())
    return n, hit_rate, z_score, mean_move_in_favor


def evaluate_trend_following_hypothesis(
    engine: Engine, broker: str, instrument: str, granularity: str,
    holding_bars: int = 10, regime_lookback: int = 250,
) -> TrendFollowingResult:
    candles_df = _load_candles(engine, broker, instrument, granularity)
    if len(candles_df) < regime_lookback + holding_bars:
        return TrendFollowingResult(instrument, holding_bars, 0, None, None, None)

    featured = compute_features(candles_df)
    classified = classify_regime(featured, lookback=regime_lookback)
    n, hit_rate, z_score, mean_move_in_favor = _evaluate_core(classified, holding_bars)
    return TrendFollowingResult(instrument, holding_bars, n, hit_rate, z_score, mean_move_in_favor)

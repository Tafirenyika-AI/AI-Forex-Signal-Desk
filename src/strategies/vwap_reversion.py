"""Strategy E — VWAP Mean Reversion (src/strategies/registry.py's "E").

A direct, honest statistical test of the strategy's own hypothesis: does a
price that deviates statistically far from its own rolling VWAP tend to
revert, under a RANGE/calm regime (not TREND, not SHOCK — reversion
against an established trend is exactly the failure mode the registry
spec calls out)?

Reuses src/features/equity_cross_market.py's `approx_vwap()` directly (the
same Level-I, disclosed-approximation rolling VWAP Equity V2 Phase 8
already built) rather than deriving a new VWAP computation — this
strategy's spec was flagged as needing "a new, real, volume-weighted
intraday feature," and that feature already existed, just hadn't been
used for this hypothesis yet.

Operates on H1 candles, same granularity `approx_vwap`/Equity V2's own
cross-market features already use.
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
from src.features.equity_cross_market import DEFAULT_LOOKBACK_BARS, approx_vwap
from src.models.regime import REGIME_RANGE, classify_regime


@dataclass(frozen=True)
class VwapReversionResult:
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
                candles_table.c.low, candles_table.c.close, candles_table.c.volume,
            )
            .where(
                candles_table.c.broker == broker, candles_table.c.instrument == instrument,
                candles_table.c.granularity == granularity, candles_table.c.complete == True,  # noqa: E712
            )
            .order_by(candles_table.c.time)
        ).all()
    return (
        pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
        .drop_duplicates(subset="time").reset_index(drop=True)
    )


def _evaluate_core(
    classified_with_vwap: pd.DataFrame, deviation_std_threshold: float, holding_bars: int,
) -> tuple[int, float | None, float | None, float | None]:
    """classified_with_vwap must have regime/regime_low_volatility/close/
    vwap_distance columns. Entry: |vwap_distance| exceeds
    `deviation_std_threshold` trailing standard deviations AND regime is
    RANGE (the registry spec's own "suitable, non-trending, non-event
    regime" gate — SHOCK/TREND/HIGH_VOLATILITY are all excluded by only
    allowing RANGE)."""
    vwap_distance = classified_with_vwap["vwap_distance"]
    threshold = deviation_std_threshold * vwap_distance.std()
    is_range = classified_with_vwap["regime"] == REGIME_RANGE
    stretched = vwap_distance.abs() > threshold
    event = (stretched & is_range & vwap_distance.notna()).to_numpy()

    closes = classified_with_vwap["close"].to_numpy()
    log_close = np.log(closes)
    # Reversion direction: a POSITIVE deviation (price above VWAP) implies
    # a predicted move DOWN (direction -1); a negative deviation implies a
    # predicted move up.
    direction = -np.sign(vwap_distance.to_numpy())

    n_rows = len(classified_with_vwap)
    forward_log_return = np.full(n_rows, np.nan)
    valid_forward = np.arange(n_rows) < (n_rows - holding_bars)
    forward_log_return[valid_forward] = log_close[np.arange(n_rows)[valid_forward] + holding_bars] - log_close[valid_forward]

    eligible = event & ~np.isnan(forward_log_return) & (direction != 0)
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


def _build_classified_with_vwap(
    engine: Engine, broker: str, instrument: str, granularity: str, regime_lookback: int, vwap_window: int,
) -> pd.DataFrame | None:
    candles_df = _load_candles(engine, broker, instrument, granularity)
    if len(candles_df) < regime_lookback + vwap_window:
        return None
    featured = compute_features(candles_df)
    classified = classify_regime(featured, lookback=regime_lookback).reset_index(drop=True)
    vwap_series = approx_vwap(candles_df, window=vwap_window)
    classified["vwap_distance"] = (classified["close"] - vwap_series) / vwap_series.replace(0, np.nan)
    return classified


def evaluate_vwap_reversion_hypothesis(
    engine: Engine, broker: str, instrument: str, granularity: str = "H1",
    deviation_std_threshold: float = 2.0, holding_bars: int = 5, regime_lookback: int = 250,
    vwap_window: int = DEFAULT_LOOKBACK_BARS,
) -> VwapReversionResult:
    classified = _build_classified_with_vwap(engine, broker, instrument, granularity, regime_lookback, vwap_window)
    if classified is None or len(classified) < holding_bars:
        return VwapReversionResult(instrument, holding_bars, 0, None, None, None)
    n, hit_rate, z_score, mean_move_in_favor = _evaluate_core(classified, deviation_std_threshold, holding_bars)
    return VwapReversionResult(instrument, holding_bars, n, hit_rate, z_score, mean_move_in_favor)


def evaluate_vwap_reversion_with_holdout(
    engine: Engine, broker: str, instrument: str, granularity: str = "H1",
    deviation_std_threshold: float = 2.0, holding_bars: int = 5, regime_lookback: int = 250,
    vwap_window: int = DEFAULT_LOOKBACK_BARS, holdout_fraction: float = 0.2,
) -> dict[str, VwapReversionResult]:
    """V4 Priority 6 discipline applied at the moment of discovery, not
    after the fact -- same chronological, never-shuffled convention as
    every other strategy's own holdout wrapper this session."""
    classified = _build_classified_with_vwap(engine, broker, instrument, granularity, regime_lookback, vwap_window)
    if classified is None or len(classified) < holding_bars:
        empty = VwapReversionResult(instrument, holding_bars, 0, None, None, None)
        return {"development": empty, "holdout": empty}

    split_idx = int(len(classified) * (1 - holdout_fraction))
    dev_n, dev_hr, dev_z, dev_mm = _evaluate_core(classified.iloc[:split_idx], deviation_std_threshold, holding_bars)
    hold_n, hold_hr, hold_z, hold_mm = _evaluate_core(classified.iloc[split_idx:].reset_index(drop=True), deviation_std_threshold, holding_bars)
    return {
        "development": VwapReversionResult(instrument, holding_bars, dev_n, dev_hr, dev_z, dev_mm),
        "holdout": VwapReversionResult(instrument, holding_bars, hold_n, hold_hr, hold_z, hold_mm),
    }

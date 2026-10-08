"""Strategy A — Time-Series Momentum (src/strategies/registry.py's "A").

A direct, honest statistical test of the strategy's own hypothesis
(Moskowitz, Ooi & Pedersen 2012): does an instrument's own trailing return
over a lookback window predict the SIGN of its return over a following
holding window? This is the HYPOTHESIS_TESTED stage specifically — no
transaction costs, no position sizing, no stop-loss simulation (that's a
later BACKTESTED stage reusing src/backtest/engine.py once/if this clears
its own validation bar). Purely: is the sign relationship real at all?

Nearest-timestamp matching (via pandas merge_asof) rather than a fixed bar
count, so weekends/holidays/missing bars don't silently misalign lookback/
holding windows — a real day-count lookback genuinely means that many
calendar days, whatever the actual bar spacing was.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.data.db import candles as candles_table


@dataclass(frozen=True)
class MomentumHypothesisResult:
    instrument: str
    lookback_days: int
    holding_days: int
    n: int
    hit_rate: float | None
    z_score: float | None  # vs the null hit_rate == 0.5
    mean_move_in_favor: float | None  # mean(direction * forward_log_return)


@dataclass(frozen=True)
class CurrentMomentumSignal:
    """The live, right-now reading -- no forward return exists yet (that's
    the future this signal is trying to anticipate), so this is NOT a
    hypothesis-test result, just "what does the trailing window say as of
    the latest available bar." Used by src/models/strategy_selector.py
    (V4 Priority 5)."""
    instrument: str
    as_of: object | None  # pandas Timestamp of the latest bar, or None if no data
    trailing_return: float | None
    direction: int | None  # +1/-1, None if no real signal (zero trailing return or insufficient data)


def _load_closes(engine: Engine, broker: str, instrument: str, granularity: str) -> pd.DataFrame:
    with engine.connect() as conn:
        rows = conn.execute(
            select(candles_table.c.time, candles_table.c.close)
            .where(
                candles_table.c.broker == broker,
                candles_table.c.instrument == instrument,
                candles_table.c.granularity == granularity,
                candles_table.c.complete == True,  # noqa: E712
            )
            .order_by(candles_table.c.time)
        ).all()
    return pd.DataFrame(rows, columns=["time", "close"]).drop_duplicates(subset="time")


def evaluate_momentum_hypothesis(
    engine: Engine, broker: str, instrument: str, granularity: str,
    lookback_days: int, holding_days: int, entry_noise_floor_std: float = 0.0,
) -> MomentumHypothesisResult:
    """Pure research test against real stored candle history -- no broker
    call, no cost model, no sizing. `entry_noise_floor_std`: only count a
    row as a real "entry" if |trailing return| exceeds this many trailing
    standard deviations of trailing returns (0.0 = every row with a
    non-zero trailing return counts, the loosest possible filter)."""
    closes = _load_closes(engine, broker, instrument, granularity)
    if len(closes) < 10:
        return MomentumHypothesisResult(instrument, lookback_days, holding_days, 0, None, None, None)

    closes["log_close"] = np.log(closes["close"])
    trail_target = closes[["time"]].copy()
    trail_target["lookback_time"] = trail_target["time"] - timedelta(days=lookback_days)
    trailing = pd.merge_asof(
        trail_target.sort_values("lookback_time"), closes[["time", "log_close"]].rename(
            columns={"time": "lookback_time", "log_close": "log_close_trail"}
        ),
        on="lookback_time", direction="nearest",
    ).sort_values("time")

    fwd_target = closes[["time"]].copy()
    fwd_target["holding_time"] = fwd_target["time"] + timedelta(days=holding_days)
    forward = pd.merge_asof(
        fwd_target.sort_values("holding_time"), closes[["time", "log_close"]].rename(
            columns={"time": "holding_time", "log_close": "log_close_fwd"}
        ),
        on="holding_time", direction="nearest",
    ).sort_values("time")

    merged = closes[["time", "log_close"]].merge(trailing[["time", "log_close_trail"]], on="time").merge(
        forward[["time", "log_close_fwd"]], on="time"
    )
    merged["trailing_return"] = merged["log_close"] - merged["log_close_trail"]
    merged["forward_return"] = merged["log_close_fwd"] - merged["log_close"]

    noise_floor = entry_noise_floor_std * merged["trailing_return"].std() if entry_noise_floor_std > 0 else 0.0
    eligible = merged[merged["trailing_return"].abs() > noise_floor].copy()
    eligible = eligible[eligible["forward_return"] != 0]  # exclude rows where no later bar exists at all (holding window beyond available history collapses to the same bar)

    n = len(eligible)
    if n == 0:
        return MomentumHypothesisResult(instrument, lookback_days, holding_days, 0, None, None, None)

    direction = np.sign(eligible["trailing_return"])
    hit = (direction * eligible["forward_return"]) > 0
    hit_rate = float(hit.mean())
    # Normal approximation to a binomial proportion test against p=0.5 --
    # standard, simple, appropriate given these sample sizes (typically
    # hundreds to low thousands of overlapping observations).
    se = math.sqrt(0.25 / n)
    z_score = (hit_rate - 0.5) / se if se > 0 else None
    mean_move_in_favor = float((direction * eligible["forward_return"]).mean())

    return MomentumHypothesisResult(instrument, lookback_days, holding_days, n, hit_rate, z_score, mean_move_in_favor)


def current_momentum_signal(
    engine: Engine, broker: str, instrument: str, granularity: str, lookback_days: int = 84,
) -> CurrentMomentumSignal:
    """The live reading for `instrument` as of its latest available bar,
    using the SAME lookback (84 days) `evaluate_momentum_hypothesis` found
    real, significant evidence for (z=2.55/3.34/6.32 across NVDA/AAPL/MSFT
    at this exact config, docs/V4_STRATEGY_RESEARCH.md Section 3) -- the
    default here intentionally matches the validated configuration, not an
    arbitrary choice."""
    closes = _load_closes(engine, broker, instrument, granularity)
    if len(closes) < 10:
        return CurrentMomentumSignal(instrument, None, None, None)

    closes = closes.sort_values("time").reset_index(drop=True)
    closes["log_close"] = np.log(closes["close"])
    latest_time = closes["time"].iloc[-1]
    lookback_time = latest_time - timedelta(days=lookback_days)
    nearest_idx = (closes["time"] - lookback_time).abs().idxmin()
    if nearest_idx == len(closes) - 1:
        # The nearest bar to "lookback_days ago" IS the latest bar itself --
        # not enough real history to measure a trailing window at all.
        return CurrentMomentumSignal(instrument, latest_time, None, None)

    trailing_return = float(closes["log_close"].iloc[-1] - closes["log_close"].iloc[nearest_idx])
    direction = int(np.sign(trailing_return)) if trailing_return != 0 else None
    return CurrentMomentumSignal(instrument, latest_time, trailing_return, direction)

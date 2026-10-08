"""Strategy F — Volatility Breakout (src/strategies/registry.py's "F").

A direct, honest statistical test of the strategy's own hypothesis: does a
period of compressed volatility (src/models/regime.py's `regime_low_volatility`,
added in V4 Priority 2) get followed by an expansion whose INITIAL direction
persists more often than chance? Reuses src/features/engine.py's
compute_features() and src/models/regime.py's classify_regime() wholesale —
this strategy is explicitly designed (per its own registry spec) to need no
new data or features, only a new event-detection + statistical test on top
of what V4 Priority 2 already built.

Pure hypothesis test only (normal-approximation z-test), same HYPOTHESIS_
TESTED scope as Strategy A — no transaction costs, no sizing, no stops.

Split into a pure, directly-testable core (`_evaluate_core`, operating on an
already-classified DataFrame) and a thin real-data wrapper
(`evaluate_volatility_breakout_hypothesis`) deliberately: classify_regime()'s
trailing percentiles are measured against a long (default 250-bar) window,
which makes hand-building a realistic end-to-end OHLC fixture with a known
answer fragile and indirect. Testing `_evaluate_core` against a directly
fabricated classified-style DataFrame (regime_low_volatility/vol_percentile/
log_return_1/close given outright, not derived) is the honest way to verify
this module's OWN logic — classify_regime() already has its own dedicated
tests (tests/test_regime_v4_detail_columns.py) and doesn't need re-proving
here.
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
from src.models.regime import classify_regime


@dataclass(frozen=True)
class VolatilityBreakoutResult:
    instrument: str
    compression_window_bars: int
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


def _evaluate_core(
    classified: pd.DataFrame, compression_window_bars: int, expansion_vol_threshold: float, holding_bars: int,
) -> tuple[int, float | None, float | None, float | None]:
    """classified must already have regime_low_volatility/vol_percentile/
    log_return_1/close columns (classify_regime()'s own output shape).
    Returns (n, hit_rate, z_score, mean_move_in_favor)."""
    # A compression event occurred at any point in the `compression_window_bars`
    # immediately BEFORE t (shift(1) excludes t itself -- the compression must
    # have already ended by the time expansion is detected, not be the same bar).
    # .fillna(False) before .astype(bool) is NOT redundant, same NaN-coercion
    # family as the shift() bug documented below: the very first row (and
    # any row before compression_window_bars of history exists) has no
    # prior data at all, so rolling().max() over an all-NaN window stays
    # NaN -- and NaN.astype(bool) evaluates True, not False, incorrectly
    # treating "no history yet" as "yes, recently compressed." Found by a
    # failing test (a series with regime_low_volatility always False still
    # produced one false-positive event at row 0).
    was_compressed_recently = (
        classified["regime_low_volatility"].shift(1).rolling(compression_window_bars, min_periods=1)
        .max().fillna(False).astype(bool)
    )
    expanding_now = (~classified["regime_low_volatility"]) & (classified["vol_percentile"] >= expansion_vol_threshold)
    qualifies = was_compressed_recently & expanding_now & classified["log_return_1"].notna() & (classified["log_return_1"] != 0)
    # Rising-edge only: vol_percentile typically stays elevated for several
    # bars after a real expansion begins, so `qualifies` alone would fire on
    # every one of those bars -- diluting "the expansion's INITIAL
    # direction" (the hypothesis this strategy actually tests) into "any
    # bar during an already-under-way expansion." Found via a failing
    # synthetic test before this ever ran against real data: a deliberately
    # REVERSING fixture still produced a 100% hit rate, because most
    # "events" were really continuation bars correlating with themselves.
    #
    # .astype(bool) after shift() is NOT redundant: shift() on a bool Series
    # introduces a leading NaN, silently upcasting the whole Series to
    # OBJECT dtype holding Python True/False/NaN -- and `~` on an
    # object-dtype Series of Python bools does integer bitwise-not
    # (~True == -2, ~False == -1), not logical negation. Found the same way
    # (the "fix" above silently did nothing until this was added too).
    not_qualifying_prev_bar = ~qualifies.shift(1).fillna(False).astype(bool)
    event = (qualifies & not_qualifying_prev_bar).to_numpy()

    closes = classified["close"].to_numpy()
    log_close = np.log(closes)
    direction = np.sign(classified["log_return_1"].to_numpy())

    n_rows = len(classified)
    forward_log_return = np.full(n_rows, np.nan)
    valid_forward = np.arange(n_rows) < (n_rows - holding_bars)
    forward_log_return[valid_forward] = log_close[np.arange(n_rows)[valid_forward] + holding_bars] - log_close[valid_forward]

    eligible = event & ~np.isnan(forward_log_return)
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


def evaluate_volatility_breakout_hypothesis(
    engine: Engine, broker: str, instrument: str, granularity: str,
    compression_window_bars: int = 20, expansion_vol_threshold: float = 0.5,
    holding_bars: int = 5, regime_lookback: int = 250,
) -> VolatilityBreakoutResult:
    candles_df = _load_candles(engine, broker, instrument, granularity)
    if len(candles_df) < regime_lookback + compression_window_bars + holding_bars:
        return VolatilityBreakoutResult(instrument, compression_window_bars, holding_bars, 0, None, None, None)

    featured = compute_features(candles_df)
    classified = classify_regime(featured, lookback=regime_lookback)
    n, hit_rate, z_score, mean_move_in_favor = _evaluate_core(
        classified, compression_window_bars, expansion_vol_threshold, holding_bars,
    )
    return VolatilityBreakoutResult(
        instrument, compression_window_bars, holding_bars, n, hit_rate, z_score, mean_move_in_favor,
    )

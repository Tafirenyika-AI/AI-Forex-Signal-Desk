"""Strategy D — Opening-Range Breakout (src/strategies/registry.py's "D").

A direct, honest statistical test of the strategy's own hypothesis: does
a volume-confirmed break of the first N minutes' opening range persist
through the rest of the regular session, more often than it falsely
reverses?

Real data constraint, disclosed: this project's own M15 backfill only
retains 60 days (src/scripts/backfill_candles.py's TARGET_LOOKBACK) —
genuinely usable for a first real test (~60 real trading days, confirmed
live), but not a long multi-year validation, per the registry's own spec.

Session open is hardcoded to 13:30 UTC (9:30 AM ET) — correct for the real
backfilled window checked live (2026-07-08 to 2026-10-08, entirely within
US Eastern Daylight Time); a DST-aware session boundary would be needed
for a test spanning a DST transition, not done here (disclosed, not
silently wrong for the window this was actually validated against).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import time as dt_time

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.data.db import candles as candles_table

SESSION_OPEN_UTC = dt_time(13, 30)


@dataclass(frozen=True)
class OpeningRangeBreakoutResult:
    instrument: str
    n: int
    hit_rate: float | None
    z_score: float | None
    mean_move_in_favor: float | None


def _load_m15_candles(engine: Engine, broker: str, instrument: str) -> pd.DataFrame:
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                candles_table.c.time, candles_table.c.high, candles_table.c.low,
                candles_table.c.close, candles_table.c.volume,
            )
            .where(
                candles_table.c.broker == broker, candles_table.c.instrument == instrument,
                candles_table.c.granularity == "M15", candles_table.c.complete == True,  # noqa: E712
            )
            .order_by(candles_table.c.time)
        ).all()
    return pd.DataFrame(rows, columns=["time", "high", "low", "close", "volume"]).drop_duplicates(subset="time")


def _evaluate_core(
    daily_groups: list[pd.DataFrame], opening_range_bars: int, volume_multiple: float,
) -> tuple[int, float | None, float | None, float | None]:
    """daily_groups: one DataFrame per real trading day, already filtered
    to bars at/after SESSION_OPEN_UTC and sorted by time, with high/low/
    close/volume columns. Detects the FIRST qualifying breakout bar each
    day and tests whether price keeps moving that direction through the
    day's own final close (same-session only, per the strategy's own
    spec)."""
    directions = []
    forward_changes = []

    for day_df in daily_groups:
        if len(day_df) < opening_range_bars + 2:
            continue
        opening = day_df.iloc[:opening_range_bars]
        range_high = opening["high"].max()
        range_low = opening["low"].min()
        avg_volume = opening["volume"].mean()
        if avg_volume <= 0:
            continue

        rest = day_df.iloc[opening_range_bars:]
        breakout_up = (rest["close"] > range_high) & (rest["volume"] > volume_multiple * avg_volume)
        breakout_down = (rest["close"] < range_low) & (rest["volume"] > volume_multiple * avg_volume)
        qualifying = rest[breakout_up | breakout_down]
        if qualifying.empty:
            continue

        first = qualifying.iloc[0]
        direction = 1 if first["close"] > range_high else -1
        final_close = day_df["close"].iloc[-1]
        forward_change = final_close - first["close"]
        if forward_change == 0:
            continue
        directions.append(direction)
        forward_changes.append(forward_change)

    n = len(directions)
    if n == 0:
        return 0, None, None, None

    direction_arr = np.array(directions, dtype=float)
    forward_arr = np.array(forward_changes, dtype=float)
    hit = (direction_arr * forward_arr) > 0
    hit_rate = float(hit.mean())
    se = math.sqrt(0.25 / n)
    z_score = (hit_rate - 0.5) / se
    mean_move_in_favor = float((direction_arr * forward_arr).mean())
    return n, hit_rate, z_score, mean_move_in_favor


def _load_daily_groups(engine: Engine, broker: str, instrument: str) -> list[pd.DataFrame]:
    candles_df = _load_m15_candles(engine, broker, instrument)
    if candles_df.empty:
        return []
    candles_df["date"] = candles_df["time"].dt.date
    candles_df = candles_df[candles_df["time"].dt.time >= SESSION_OPEN_UTC]
    dates_sorted = sorted(candles_df["date"].unique())
    return [candles_df[candles_df["date"] == d].sort_values("time") for d in dates_sorted]


def evaluate_opening_range_breakout_hypothesis(
    engine: Engine, broker: str, instrument: str, opening_range_bars: int = 2, volume_multiple: float = 1.2,
) -> OpeningRangeBreakoutResult:
    daily_groups = _load_daily_groups(engine, broker, instrument)
    n, hit_rate, z_score, mean_move_in_favor = _evaluate_core(daily_groups, opening_range_bars, volume_multiple)
    return OpeningRangeBreakoutResult(instrument, n, hit_rate, z_score, mean_move_in_favor)


def evaluate_opening_range_breakout_with_holdout(
    engine: Engine, broker: str, instrument: str, opening_range_bars: int = 2, volume_multiple: float = 1.2,
    holdout_fraction: float = 0.2,
) -> dict[str, OpeningRangeBreakoutResult]:
    """V4 Priority 6 discipline applied at the moment of discovery, not
    after the fact -- chronological, never-shuffled split by real trading
    day, same convention as every other strategy's own holdout wrapper
    this session."""
    daily_groups = _load_daily_groups(engine, broker, instrument)
    split_idx = int(len(daily_groups) * (1 - holdout_fraction))
    dev_n, dev_hr, dev_z, dev_mm = _evaluate_core(daily_groups[:split_idx], opening_range_bars, volume_multiple)
    hold_n, hold_hr, hold_z, hold_mm = _evaluate_core(daily_groups[split_idx:], opening_range_bars, volume_multiple)
    return {
        "development": OpeningRangeBreakoutResult(instrument, dev_n, dev_hr, dev_z, dev_mm),
        "holdout": OpeningRangeBreakoutResult(instrument, hold_n, hold_hr, hold_z, hold_mm),
    }

"""Strategy B — Cross-Sectional Momentum (src/strategies/registry.py's "B").

A direct, honest statistical test of the strategy's own hypothesis
(Jegadeesh & Titman 1993): ranking a cross-section of instruments by
trailing ABSOLUTE return and buying the top tier / selling the bottom
tier produces a positive long-short spread return over the following
holding period.

Deliberately NOT relative-to-benchmark (unlike src/strategies/
sector_rotation.py's Strategy H, which ranks sector ETFs' return RELATIVE
TO SPY) — Strategy B's classic construction ranks on absolute trailing
return and tests the top-minus-bottom SPREAD directly, the actual
Jegadeesh-Titman long-short portfolio, a different test from H's.

Cross-section: the real instruments this project currently has backfilled
H4 history for that share the same US-equity trading calendar (exact-
timestamp alignment, same reasoning as sector_rotation.py) — 3 individual
equities + 10 ETFs as of 2026-10-08. Crypto is deliberately excluded here
(different trading calendar, would break exact-timestamp alignment; also
not what this strategy's spec names as its universe).

Pure hypothesis test only — no transaction costs, no sizing. A disclosed,
small-cross-section limitation: academic cross-sectional momentum studies
use hundreds of names; this project's own real, currently-backfilled
universe is much smaller — reported honestly, not padded.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.data.db import candles as candles_table


@dataclass(frozen=True)
class CrossSectionalMomentumResult:
    n: int
    n_instruments_covered: int
    mean_spread_return: float | None  # mean(top-tier forward return) - mean(bottom-tier forward return)
    t_statistic: float | None


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


def _evaluate_core(
    trailing_df: pd.DataFrame, forward_df: pd.DataFrame, top_tier_fraction: float,
) -> CrossSectionalMomentumResult:
    """Pure core: given already-aligned per-instrument trailing/forward
    ABSOLUTE return frames (same index, one column per instrument), ranks
    each eligible timestamp's instruments into top/bottom tiers by
    trailing return and collects the forward spread (top minus bottom)."""
    n_instruments = trailing_df.shape[1]
    n_tier = max(1, int(n_instruments * top_tier_fraction))
    spreads: list[float] = []
    for t in trailing_df.index:
        trailing_row = trailing_df.loc[t]
        if trailing_row.isna().any():
            continue
        forward_row = forward_df.loc[t]
        if forward_row.isna().any():
            continue
        ranked = trailing_row.sort_values(ascending=False)
        top = ranked.index[:n_tier]
        bottom = ranked.index[-n_tier:]
        spread = forward_row[top].mean() - forward_row[bottom].mean()
        spreads.append(spread)

    n = len(spreads)
    if n < 2:
        return CrossSectionalMomentumResult(n, n_instruments, None, None)

    arr = np.array(spreads)
    mean_spread = float(arr.mean())
    std = float(arr.std(ddof=1))
    t_stat = mean_spread / (std / math.sqrt(n)) if std > 0 else None
    return CrossSectionalMomentumResult(n, n_instruments, mean_spread, t_stat)


def _build_trailing_forward(
    engine: Engine, broker: str, instruments: tuple[str, ...], granularity: str,
    lookback_bars: int, holding_bars: int,
) -> tuple[pd.DataFrame, pd.DataFrame, int] | None:
    closes_by_instrument: dict[str, pd.Series] = {}
    for instrument in instruments:
        s = _load_closes(engine, broker, instrument, granularity)
        if s is not None and len(s) >= lookback_bars + holding_bars:
            closes_by_instrument[instrument] = s
    if len(closes_by_instrument) < 4:  # need a real cross-section to form distinct top/bottom tiers
        return None

    common_index = None
    for s in closes_by_instrument.values():
        common_index = s.index if common_index is None else common_index.intersection(s.index)
    common_index = common_index.sort_values()
    if len(common_index) < lookback_bars + holding_bars + 1:
        return None

    trailing = {}
    forward = {}
    for instrument, s in closes_by_instrument.items():
        aligned = s.reindex(common_index)
        trailing[instrument] = aligned.pct_change(lookback_bars)
        forward[instrument] = aligned.shift(-holding_bars) / aligned - 1

    return pd.DataFrame(trailing), pd.DataFrame(forward), len(closes_by_instrument)


def evaluate_cross_sectional_momentum_hypothesis(
    engine: Engine, broker: str, instruments: tuple[str, ...], granularity: str,
    lookback_bars: int = 20, holding_bars: int = 20, top_tier_fraction: float = 1 / 3,
) -> CrossSectionalMomentumResult:
    built = _build_trailing_forward(engine, broker, instruments, granularity, lookback_bars, holding_bars)
    if built is None:
        return CrossSectionalMomentumResult(0, 0, None, None)
    trailing_df, forward_df, n_covered = built
    result = _evaluate_core(trailing_df, forward_df, top_tier_fraction)
    return CrossSectionalMomentumResult(result.n, n_covered, result.mean_spread_return, result.t_statistic)


def evaluate_cross_sectional_momentum_with_holdout(
    engine: Engine, broker: str, instruments: tuple[str, ...], granularity: str,
    lookback_bars: int = 20, holding_bars: int = 20, top_tier_fraction: float = 1 / 3,
    holdout_fraction: float = 0.2,
) -> dict[str, CrossSectionalMomentumResult]:
    """V4 Priority 6 discipline applied directly to a new finding, not
    after the fact: chronological, never-shuffled development/holdout
    split, same convention as src/strategies/time_series_momentum.py's
    evaluate_momentum_hypothesis_with_holdout()."""
    built = _build_trailing_forward(engine, broker, instruments, granularity, lookback_bars, holding_bars)
    if built is None:
        empty = CrossSectionalMomentumResult(0, 0, None, None)
        return {"development": empty, "holdout": empty}
    trailing_df, forward_df, n_covered = built

    split_idx = int(len(trailing_df) * (1 - holdout_fraction))
    dev_result = _evaluate_core(trailing_df.iloc[:split_idx], forward_df.iloc[:split_idx], top_tier_fraction)
    hold_result = _evaluate_core(trailing_df.iloc[split_idx:], forward_df.iloc[split_idx:], top_tier_fraction)
    return {
        "development": CrossSectionalMomentumResult(dev_result.n, n_covered, dev_result.mean_spread_return, dev_result.t_statistic),
        "holdout": CrossSectionalMomentumResult(hold_result.n, n_covered, hold_result.mean_spread_return, hold_result.t_statistic),
    }

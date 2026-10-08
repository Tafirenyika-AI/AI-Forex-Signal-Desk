"""Strategy H — Sector Rotation (src/strategies/registry.py's "H").

A direct, honest statistical test of the strategy's own hypothesis: does a
sector ETF's trailing relative strength vs. SPY (top-tier of the 11 SPDR
sector ETFs, src/equity/relationships.py's SIC_TO_SECTOR) predict continued
OUTPERFORMANCE vs. SPY over the following rotation window, conditioned on
the broad-market regime (src/models/regime.py, applied to SPY itself) not
being SHOCK? Reuses the already-backfilled SPY + 11 sector ETF history
(V4 Priority 2's BENCHMARK_INSTRUMENTS) directly — no new data needed, per
the strategy's own registry spec.

Unlike Strategy A/C/F (a binary hit-rate z-test), this tests whether the
MEAN forward relative return of top-tier selections is significantly
positive — a one-sample t-test, since the quantity under test (continued
relative outperformance) is itself continuous and the natural null is
"zero excess return," not "50% hit rate."

All ETFs share the same US-equity trading calendar as SPY, so alignment is
exact-timestamp (not Strategy A's cross-asset nearest-day merge_asof,
needed there because forex/crypto don't share equities' session grid).

Pure hypothesis test only — no transaction costs, no sizing.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.data.db import candles as candles_table
from src.equity.relationships import SIC_TO_SECTOR
from src.features.engine import compute_features
from src.models.regime import REGIME_SHOCK, classify_regime

SECTOR_ETFS: tuple[str, ...] = tuple(sorted({etf for _, (_, etf) in SIC_TO_SECTOR}))


@dataclass(frozen=True)
class SectorRotationResult:
    n: int
    n_etfs_covered: int
    mean_forward_relative_return: float | None
    t_statistic: float | None


def _load_closes(engine: Engine, broker: str, instrument: str, granularity: str) -> pd.Series | None:
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
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["time", "close"]).drop_duplicates(subset="time").set_index("time")
    return df["close"]


def _evaluate_core(
    trailing_df: pd.DataFrame, forward_df: pd.DataFrame, not_shock: pd.Series,
    top_tier_fraction: float,
) -> SectorRotationResult:
    """Pure core: given already-aligned per-ETF relative-trailing-return and
    relative-forward-return frames (same index, one column per ETF) plus a
    per-timestamp "broad market not in SHOCK" boolean Series, ranks each
    eligible timestamp's ETFs by trailing relative strength and collects
    the top tier's forward relative returns. Directly testable without any
    DB/regime-classification round trip."""
    n_etfs = trailing_df.shape[1]
    n_top = max(1, int(n_etfs * top_tier_fraction))
    observations: list[float] = []
    for t in trailing_df.index:
        trailing_row = trailing_df.loc[t]
        if trailing_row.isna().any():
            continue
        if t not in not_shock.index or not bool(not_shock.get(t, False)):
            continue
        top_etfs = trailing_row.nlargest(n_top).index
        forward_row = forward_df.loc[t, top_etfs]
        if forward_row.isna().any():
            continue
        observations.extend(forward_row.tolist())

    n = len(observations)
    if n < 2:
        return SectorRotationResult(n, n_etfs, None, None)

    arr = np.array(observations)
    mean_forward = float(arr.mean())
    std = float(arr.std(ddof=1))
    t_stat = mean_forward / (std / math.sqrt(n)) if std > 0 else None
    return SectorRotationResult(n, n_etfs, mean_forward, t_stat)


def evaluate_sector_rotation_hypothesis(
    engine: Engine, broker: str, granularity: str,
    lookback_bars: int = 20, holding_bars: int = 20, top_tier_fraction: float = 1 / 3,
    regime_lookback: int = 250,
) -> SectorRotationResult:
    spy_close = _load_closes(engine, broker, "SPY", granularity)
    if spy_close is None or len(spy_close) < regime_lookback + max(lookback_bars, holding_bars):
        return SectorRotationResult(0, 0, None, None)

    sector_closes: dict[str, pd.Series] = {}
    for etf in SECTOR_ETFS:
        s = _load_closes(engine, broker, etf, granularity)
        if s is not None and len(s) >= lookback_bars + holding_bars:
            sector_closes[etf] = s
    if len(sector_closes) < 3:  # need a real cross-section to rank, not just 1-2 ETFs
        return SectorRotationResult(0, len(sector_closes), None, None)

    # Broad-market regime, from SPY's own OHLC -- reuse real H4 bars
    # (open/high/low/close) rather than fabricating them from close alone.
    with engine.connect() as conn:
        spy_ohlc_rows = conn.execute(
            select(
                candles_table.c.time, candles_table.c.open, candles_table.c.high,
                candles_table.c.low, candles_table.c.close,
            )
            .where(
                candles_table.c.broker == broker, candles_table.c.instrument == "SPY",
                candles_table.c.granularity == granularity, candles_table.c.complete == True,  # noqa: E712
            )
            .order_by(candles_table.c.time)
        ).all()
    spy_ohlc = pd.DataFrame(spy_ohlc_rows, columns=["time", "open", "high", "low", "close"]).drop_duplicates(subset="time")
    spy_classified = classify_regime(compute_features(spy_ohlc), lookback=regime_lookback)
    not_shock = (spy_classified.set_index(spy_ohlc["time"])["regime"] != REGIME_SHOCK)

    # Common time index across SPY + every covered sector ETF -- exact
    # timestamp alignment, not a nearest-match merge (same US-equity
    # trading calendar for all of them).
    common_index = spy_close.index
    for s in sector_closes.values():
        common_index = common_index.intersection(s.index)
    common_index = common_index.sort_values()
    if len(common_index) < lookback_bars + holding_bars + 1:
        return SectorRotationResult(0, len(sector_closes), None, None)

    spy_aligned = spy_close.reindex(common_index)
    spy_trailing_ret = spy_aligned.pct_change(lookback_bars)
    spy_forward_ret = spy_aligned.shift(-holding_bars) / spy_aligned - 1

    relative_trailing = {}
    relative_forward = {}
    for etf, s in sector_closes.items():
        aligned = s.reindex(common_index)
        trailing_ret = aligned.pct_change(lookback_bars)
        forward_ret = aligned.shift(-holding_bars) / aligned - 1
        relative_trailing[etf] = trailing_ret - spy_trailing_ret
        relative_forward[etf] = forward_ret - spy_forward_ret

    trailing_df = pd.DataFrame(relative_trailing)
    forward_df = pd.DataFrame(relative_forward)

    return _evaluate_core(trailing_df, forward_df, not_shock, top_tier_fraction)

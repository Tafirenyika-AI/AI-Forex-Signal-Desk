"""Regime detector (blueprint sec. 3 layer 5).

v1 is rule-based and self-relative: every threshold is a percentile against
the instrument's own trailing history, not a fixed magic number, so it
adapts automatically across pairs with very different typical volatility
(e.g. JPY crosses vs EUR/USD). A learned HMM/clustering regime model is a
reasonable v1.1 upgrade once there's enough labeled regime history to
validate it against — not needed to get a working system running today.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

REGIME_TREND = "TREND"
REGIME_RANGE = "RANGE"
REGIME_SHOCK = "SHOCK"
REGIME_HIGH_VOL = "HIGH_VOLATILITY"

SHOCK_MULTIPLIER = 4.0
HIGH_VOL_PERCENTILE = 0.85
TREND_PERCENTILE = 0.65


def _trailing_percentile(series: pd.Series, lookback: int) -> pd.Series:
    """Causal percentile rank of each value against its own trailing window."""
    min_periods = max(10, lookback // 5)

    def rank_last(window: np.ndarray) -> float:
        return (window[-1] >= window).mean()

    return series.rolling(lookback, min_periods=min_periods).apply(rank_last, raw=True)


def classify_regime(featured_df: pd.DataFrame, lookback: int = 250) -> pd.DataFrame:
    """Adds a `regime` column. Requires compute_features() output (log_return_1,
    rolling_vol_20, trend_slope) already present. Every input to a given row's
    label comes only from that row and earlier rows."""
    out = featured_df.copy()

    vol_rank = _trailing_percentile(out["rolling_vol_20"], lookback)
    trend_rank = _trailing_percentile(out["trend_slope"].abs(), lookback)
    shock_flag = out["log_return_1"].abs() > (out["rolling_vol_20"] * SHOCK_MULTIPLIER)

    regime = np.select(
        [
            shock_flag,
            vol_rank >= HIGH_VOL_PERCENTILE,
            trend_rank >= TREND_PERCENTILE,
        ],
        [REGIME_SHOCK, REGIME_HIGH_VOL, REGIME_TREND],
        default=REGIME_RANGE,
    )

    out["vol_percentile"] = vol_rank
    out["trend_percentile"] = trend_rank
    out["regime"] = regime
    # Rows still inside the trailing lookback warm-up window have no valid
    # percentile yet — mark them explicitly rather than defaulting to RANGE.
    warm_up = vol_rank.isna() | trend_rank.isna()
    out.loc[warm_up, "regime"] = "UNKNOWN"
    return out


def current_regime(featured_df: pd.DataFrame, lookback: int = 250) -> str:
    classified = classify_regime(featured_df, lookback=lookback)
    return str(classified["regime"].iloc[-1])

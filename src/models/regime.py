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

# V4 Priority 2 (docs/V4_ARCHITECTURE.md Section 8 gap): directional/detail
# annotations added ALONGSIDE the hard `regime` label above, never replacing
# any of its values. src/decision/fusion.py's REGIME_WEIGHT_MULTIPLIERS is
# live, execution-adjacent code keyed on today's exact label set — an
# unrecognized key silently falls through to a neutral no-adjustment
# default, so widening what `regime` itself can be would silently change
# real fuse() weighting for every live trade. These new values are only
# ever written to the new columns below.
REGIME_TREND_UP = "TREND_UP"
REGIME_TREND_DOWN = "TREND_DOWN"
REGIME_LOW_VOL = "LOW_VOLATILITY"
REGIME_EVENT_DRIVEN = "EVENT_DRIVEN"

SHOCK_MULTIPLIER = 4.0
HIGH_VOL_PERCENTILE = 0.85
LOW_VOL_PERCENTILE = 0.15  # symmetric to HIGH_VOL_PERCENTILE
TREND_PERCENTILE = 0.65


def _trailing_percentile(series: pd.Series, lookback: int) -> pd.Series:
    """Causal percentile rank of each value against its own trailing window."""
    min_periods = max(10, lookback // 5)

    def rank_last(window: np.ndarray) -> float:
        return (window[-1] >= window).mean()

    return series.rolling(lookback, min_periods=min_periods).apply(rank_last, raw=True)


def _regime_probability(regime: pd.Series, vol_rank: pd.Series, trend_rank: pd.Series) -> pd.Series:
    """Cheap, honest uncertainty proxy — how far the deciding percentile sits
    past the threshold that produced the hard label, computed directly from
    percentiles already in hand rather than a separately fitted probability
    model (exactly the "natural, cheap source" docs/V4_ARCHITECTURE.md's own
    Priority 2 note points at). 1.0 = deep in the regime's territory, 0.0 =
    sitting right at the boundary. SHOCK is binary by construction (a single
    large-return flag), so it is always 1.0 when triggered."""
    trend_margin = ((trend_rank - TREND_PERCENTILE) / (1 - TREND_PERCENTILE)).clip(lower=0, upper=1)
    vol_margin = ((vol_rank - HIGH_VOL_PERCENTILE) / (1 - HIGH_VOL_PERCENTILE)).clip(lower=0, upper=1)
    # RANGE only ever applies when both percentiles are below their
    # thresholds (the np.select default case) -- distance from whichever one
    # is closer to flipping the label.
    range_margin = (1 - np.maximum(trend_rank / TREND_PERCENTILE, vol_rank / HIGH_VOL_PERCENTILE)).clip(lower=0, upper=1)

    return pd.Series(
        np.select(
            [regime == REGIME_SHOCK, regime == REGIME_HIGH_VOL, regime == REGIME_TREND, regime == REGIME_RANGE],
            [1.0, vol_margin, trend_margin, range_margin],
            default=0.0,
        ),
        index=regime.index,
    )


def classify_regime(
    featured_df: pd.DataFrame, lookback: int = 250, event_flag: pd.Series | None = None,
) -> pd.DataFrame:
    """Adds a `regime` column. Requires compute_features() output (log_return_1,
    rolling_vol_20, trend_slope) already present. Every input to a given row's
    label comes only from that row and earlier rows.

    `event_flag`: optional boolean Series aligned to `featured_df`'s index,
    True where a real corporate event (e.g. an earnings-lockout window from
    src/risk/equity_governor_extensions.py's upcoming_earnings_lockout_gate /
    the company_events table) is active for that row. Defaults to None, which
    makes `regime_event_driven` always False — every existing caller that
    doesn't pass this gets byte-identical output to before this parameter
    existed. Wiring a real per-symbol event feed into the live equity feature
    pipeline (equity_engine.py / equity_vectorized.py) is deliberately NOT
    done in this pass — disclosed as a known limitation, not silently
    skipped; see docs/V4_IMPLEMENTATION_LOG.md's Priority 2 entry."""
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

    # --- V4 Priority 2 additive detail columns -- informational only, never
    # read by fuse() or any live decision path; see module-level comment on
    # REGIME_TREND_UP et al. above for why these can't just widen `regime`. ---
    is_trend = out["regime"] == REGIME_TREND
    # Not-trending rows get pandas' missing marker (NaN) here, not a literal
    # None -- pandas 3.x's default string-dtype inference for an
    # otherwise-all-strings column silently turns None into NaN at
    # construction time regardless of how it's built, so check with
    # pd.isna(), not `is None`.
    out["regime_direction"] = np.where(
        is_trend, np.where(out["trend_slope"] > 0, REGIME_TREND_UP, REGIME_TREND_DOWN), None,
    )
    out["regime_low_volatility"] = (out["regime"] == REGIME_RANGE) & (vol_rank <= LOW_VOL_PERCENTILE)
    if event_flag is not None:
        out["regime_event_driven"] = event_flag.reindex(out.index).fillna(False).astype(bool)
    else:
        out["regime_event_driven"] = False
    out["regime_probability"] = _regime_probability(out["regime"], vol_rank, trend_rank)
    out.loc[warm_up, "regime_probability"] = 0.0

    return out


def current_regime(featured_df: pd.DataFrame, lookback: int = 250) -> str:
    classified = classify_regime(featured_df, lookback=lookback)
    return str(classified["regime"].iloc[-1])

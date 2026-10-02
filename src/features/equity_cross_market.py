"""Equity V2 Phase 8 — cross-market features.

Computes relative-strength/benchmark features for one equity ticker
against SPY (broad market, via src/scripts/backfill_candles.py's
BENCHMARK_INSTRUMENTS) and its own sector ETF (Phase 7's sic_to_sector),
plus single-instrument features (relative volume, overnight gap%,
volatility percentile). Causal throughout — every computation at row t
uses only bars <= t, same discipline as src/features/engine.py.

Never fabricates Level II data from Level I quotes (the brief's own
Phase 8 caution, explicit): `approx_vwap` is a standard, disclosed
approximation of VWAP from OHLCV bars (typical price = (H+L+C)/3,
volume-weighted over a trailing window) — it is NOT real tick-by-tick
VWAP, which would require Level II/trade-level data this project doesn't
have and never claims to.

Operates on H1 candles specifically (this project's own hourly
granularity — see src/scripts/backfill_candles.py's GRANULARITIES), not a
dedicated daily bar this project doesn't maintain. `gap_pct` is computed
from each UTC calendar day's first H1 bar's open vs. the prior trading
day's last H1 bar's close — an honest overnight-gap definition at the
granularity actually available, not a same-bar-to-same-bar artifact.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# A trailing window of this many H1 bars for relative-volume/volatility-
# percentile/return comparisons — roughly 2-3 trading days of hourly bars,
# long enough to not be noise-dominated by a single session, short enough
# to stay genuinely "recent" rather than smearing in month-old regime.
DEFAULT_LOOKBACK_BARS = 20


@dataclass(frozen=True)
class CrossMarketFeatures:
    instrument: str
    as_of: pd.Timestamp
    relative_volume: float | None = None
    gap_pct: float | None = None
    volatility_percentile: float | None = None
    return_vs_spy: float | None = None
    return_vs_sector_etf: float | None = None
    vwap_distance: float | None = None  # (close - approx_vwap) / approx_vwap -- Level-I approximation, see module docstring
    missing_metrics: tuple[str, ...] = field(default_factory=tuple)


def approx_vwap(df: pd.DataFrame, window: int = DEFAULT_LOOKBACK_BARS) -> pd.Series:
    """Rolling VWAP approximation from OHLCV bars -- see module docstring
    for why this is a Level-I approximation, not real VWAP."""
    typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = typical_price * df["volume"]
    volume_sum = df["volume"].rolling(window, min_periods=window).sum()
    return pv.rolling(window, min_periods=window).sum() / volume_sum.replace(0, np.nan)


def _as_of_row(df: pd.DataFrame, as_of: pd.Timestamp) -> pd.Series | None:
    eligible = df[df["time"] <= as_of]
    if eligible.empty:
        return None
    return eligible.iloc[-1]


def _window_return(df: pd.DataFrame, as_of: pd.Timestamp, lookback_bars: int) -> float | None:
    eligible = df[df["time"] <= as_of].reset_index(drop=True)
    if len(eligible) <= lookback_bars:
        return None
    recent_close = eligible["close"].iloc[-1]
    past_close = eligible["close"].iloc[-1 - lookback_bars]
    if past_close == 0:
        return None
    return (recent_close - past_close) / past_close


def _gap_pct(df: pd.DataFrame, as_of: pd.Timestamp) -> float | None:
    eligible = df[df["time"] <= as_of].copy()
    if eligible.empty:
        return None
    eligible["date"] = eligible["time"].dt.date
    days = eligible["date"].unique()
    if len(days) < 2:
        return None
    today, prior_day = days[-1], days[-2]
    today_open = eligible[eligible["date"] == today]["open"].iloc[0]
    prior_close = eligible[eligible["date"] == prior_day]["close"].iloc[-1]
    if prior_close == 0:
        return None
    return (today_open - prior_close) / prior_close


def _volatility_percentile(df: pd.DataFrame, as_of: pd.Timestamp, lookback_bars: int) -> float | None:
    eligible = df[df["time"] <= as_of].reset_index(drop=True)
    if len(eligible) < lookback_bars:
        return None
    window = eligible.tail(lookback_bars).copy()
    window["range_pct"] = (window["high"] - window["low"]) / window["close"]
    current = window["range_pct"].iloc[-1]
    return float((window["range_pct"] <= current).mean())


def compute_cross_market_features(
    ticker_df: pd.DataFrame,
    spy_df: pd.DataFrame | None,
    sector_etf_df: pd.DataFrame | None,
    instrument: str,
    as_of: pd.Timestamp,
    lookback_bars: int = DEFAULT_LOOKBACK_BARS,
) -> CrossMarketFeatures:
    """Pure function over already-loaded H1 candle frames (each sorted
    ascending by `time`, same shape src/features/engine.py's
    load_candles_df returns) -- no DB/network access, directly testable.
    Every feature is None (never fabricated/interpolated) when its inputs
    are insufficient as-of the requested time."""
    missing: list[str] = []

    row = _as_of_row(ticker_df, as_of)
    if row is None:
        missing.extend(["relative_volume", "gap_pct", "volatility_percentile",
                         "return_vs_spy", "return_vs_sector_etf", "vwap_distance"])
        return CrossMarketFeatures(instrument=instrument, as_of=as_of, missing_metrics=tuple(missing))

    eligible = ticker_df[ticker_df["time"] <= as_of].reset_index(drop=True)

    relative_volume = None
    if len(eligible) > lookback_bars:
        avg_volume = eligible["volume"].iloc[-1 - lookback_bars:-1].mean()
        relative_volume = (eligible["volume"].iloc[-1] / avg_volume) if avg_volume else None
    if relative_volume is None:
        missing.append("relative_volume")

    gap_pct = _gap_pct(ticker_df, as_of)
    if gap_pct is None:
        missing.append("gap_pct")

    volatility_percentile = _volatility_percentile(ticker_df, as_of, lookback_bars)
    if volatility_percentile is None:
        missing.append("volatility_percentile")

    ticker_return = _window_return(ticker_df, as_of, lookback_bars)

    return_vs_spy = None
    if spy_df is not None and ticker_return is not None:
        spy_return = _window_return(spy_df, as_of, lookback_bars)
        if spy_return is not None:
            return_vs_spy = ticker_return - spy_return
    if return_vs_spy is None:
        missing.append("return_vs_spy")

    return_vs_sector_etf = None
    if sector_etf_df is not None and ticker_return is not None:
        sector_return = _window_return(sector_etf_df, as_of, lookback_bars)
        if sector_return is not None:
            return_vs_sector_etf = ticker_return - sector_return
    if return_vs_sector_etf is None:
        missing.append("return_vs_sector_etf")

    vwap_distance = None
    if len(eligible) >= lookback_bars:
        vwap_series = approx_vwap(eligible, window=lookback_bars)
        current_vwap = vwap_series.iloc[-1]
        if pd.notna(current_vwap) and current_vwap != 0:
            vwap_distance = (eligible["close"].iloc[-1] - current_vwap) / current_vwap
    if vwap_distance is None:
        missing.append("vwap_distance")

    return CrossMarketFeatures(
        instrument=instrument,
        as_of=as_of,
        relative_volume=relative_volume,
        gap_pct=gap_pct,
        volatility_percentile=volatility_percentile,
        return_vs_spy=return_vs_spy,
        return_vs_sector_etf=return_vs_sector_etf,
        vwap_distance=vwap_distance,
        missing_metrics=tuple(missing),
    )

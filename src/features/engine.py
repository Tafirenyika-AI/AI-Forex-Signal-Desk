"""Feature engine (blueprint sec. 3 layer 4 / sec. 6.2).

All indicators here are causal: the value at row t is computed only from
rows <= t. This is what keeps the feature set point-in-time correct — no
function in this module may use future rows. The one exception is
`add_forward_target`, which deliberately looks forward to build a training
label; that column must never be used as a model input.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.data.db import candles as candles_table

# Causal, produced directly by compute_features() below.
TECHNICAL_FEATURE_COLUMNS = [
    "log_return_1",
    "log_return_4",
    "log_return_12",
    "rolling_vol_20",
    "atr_14",
    "rsi_14",
    "trend_slope",
    "range_pct",
    "body_pct",
]
# NOT produced by compute_features() — these come from
# src.models.regime.classify_regime(), which every real caller already runs
# on feature_ready_frame()'s output before training/predicting (see
# run_loop.py's _evaluate_one_horizon and src/backtest/engine.py). Continuous
# percentiles, not the one-hot `regime` label itself — a GBM keeps more
# information from a continuous 0..1 rank than from a categorical bucket.
REGIME_FEATURE_COLUMNS = ["vol_percentile", "trend_percentile"]

# The full model input set (src/models/price_model.py's fit/predict_proba_up)
# — requires classify_regime() to have already run, unlike
# TECHNICAL_FEATURE_COLUMNS alone (see feature_ready_frame()'s own dropna,
# which deliberately only checks the technical set, since regime hasn't run
# yet at that point in the pipeline).
FEATURE_COLUMNS = TECHNICAL_FEATURE_COLUMNS + REGIME_FEATURE_COLUMNS


def load_candles_df(engine: Engine, instrument: str, granularity: str) -> pd.DataFrame:
    stmt = (
        select(candles_table)
        .where(candles_table.c.instrument == instrument)
        .where(candles_table.c.granularity == granularity)
        .where(candles_table.c.complete.is_(True))
        .order_by(candles_table.c.time.asc())
    )
    with engine.connect() as conn:
        df = pd.read_sql(stmt, conn)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df.reset_index(drop=True)


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50.0)


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    """Adds causal indicator columns to a candle dataframe sorted ascending by time."""
    out = df.copy()
    log_close = np.log(out["close"])

    out["log_return_1"] = log_close.diff(1)
    out["log_return_4"] = log_close.diff(4)
    out["log_return_12"] = log_close.diff(12)
    out["rolling_vol_20"] = out["log_return_1"].rolling(20, min_periods=20).std()
    out["atr_14"] = _atr(out, 14)
    out["rsi_14"] = _rsi(out["close"], 14)

    sma_10 = out["close"].rolling(10, min_periods=10).mean()
    sma_50 = out["close"].rolling(50, min_periods=50).mean()
    out["trend_slope"] = (sma_10 - sma_50) / sma_50

    out["range_pct"] = (out["high"] - out["low"]) / out["close"]
    out["body_pct"] = (out["close"] - out["open"]) / out["close"]

    return out


def add_forward_target(df: pd.DataFrame, horizon_bars: int) -> pd.DataFrame:
    """Adds `target_up`: 1 if close[t+horizon] > close[t]. Training-label only —
    never feed this column into a model as an input feature."""
    out = df.copy()
    future_close = out["close"].shift(-horizon_bars)
    out["target_up"] = (future_close > out["close"]).astype("Int64")
    out.loc[future_close.isna(), "target_up"] = pd.NA
    return out


def feature_ready_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Rows where every causal technical feature is defined (post warm-up
    window). Only TECHNICAL_FEATURE_COLUMNS — REGIME_FEATURE_COLUMNS don't
    exist yet at this point in the pipeline (classify_regime() hasn't run;
    every real caller runs it on this function's own output, then does its
    own additional dropna(subset=REGIME_FEATURE_COLUMNS) before training,
    since regime has its own, longer warm-up window)."""
    featured = compute_features(df)
    return featured.dropna(subset=TECHNICAL_FEATURE_COLUMNS).reset_index(drop=True)

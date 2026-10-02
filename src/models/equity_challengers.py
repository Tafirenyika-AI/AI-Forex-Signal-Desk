"""Equity V2 Phase 10 — equity challenger models.

Builds challenger component models (FUNDAMENTAL / NEWS_EVENT /
MARKET_REGIME) consuming the feature groups src/features/equity_engine.py
(Phase 9) defines and src/features/equity_vectorized.py (Phase 10
support) assembles across a ticker's full history, plus a META combiner
stacking all four components (the existing price model + these three new
challengers) into one probability.

SHADOW-ONLY, per the brief's own Phase 18 instruction ("challengers run
shadow-only, cannot submit broker orders; promotion requires explicit
chronological out-of-sample evidence"): nothing in this module is
imported by src/run_loop.py's live decision cycle, src/execution/
service.py, or src/decision/fusion.py. Building these models is not the
same as deploying them — that is Phase 18's job, gated on Phase 11's
walk-forward validation, never on training-set performance alone.

Every challenger mirrors src/models/price_model.py's own shape exactly
(a StandardScaler + GradientBoostingClassifier Pipeline) so Phase 11 can
evaluate all four components through one identical walk-forward harness
(src/models/price_model.py's own walk_forward_splits, reused here rather
than redefined).

This module does NOT itself enforce chronological train/test separation
— that discipline belongs to whatever calls fit()/predict_proba() (Phase
11's walk-forward harness, exactly like src/backtest/engine.py already
does for the existing price model). Calling fit() and predict_proba()
with the same rows would silently measure training performance, not
genuine out-of-sample skill — never do that when evaluating a
challenger's real-world worth.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from src.features.engine import REGIME_FEATURE_COLUMNS
from src.features.equity_vectorized import CROSS_MARKET_FEATURE_COLUMNS, FUNDAMENTAL_FEATURE_COLUMNS, NEWS_EVENT_FEATURE_COLUMNS

MARKET_REGIME_FEATURE_COLUMNS = REGIME_FEATURE_COLUMNS + ["intraday_volatility_percentile"]
# MICROSTRUCTURE/RELATIVE_STRENGTH folded into one challenger (not split
# further) -- both describe "how is this bar behaving relative to its own
# recent trading and the broader market," a single coherent signal family,
# unlike FUNDAMENTAL (company-level) or NEWS_EVENT (event-driven), which
# are genuinely distinct information sources the brief names separately.
CROSS_MARKET_CHALLENGER_COLUMNS = CROSS_MARKET_FEATURE_COLUMNS

COMPONENT_COLUMNS: dict[str, list[str]] = {
    "fundamental": FUNDAMENTAL_FEATURE_COLUMNS,
    "news_event": NEWS_EVENT_FEATURE_COLUMNS,
    "market_regime": MARKET_REGIME_FEATURE_COLUMNS,
    "cross_market": CROSS_MARKET_CHALLENGER_COLUMNS,
}


def _build_pipeline() -> Pipeline:
    # Identical hyperparameters to src/models/price_model.py's own
    # build_pipeline() -- deliberately not re-tuned per component yet;
    # Phase 11's walk-forward evidence is what should drive any future
    # per-component tuning, not a guess made while just wiring this up.
    return Pipeline([
        ("scaler", StandardScaler()),
        ("clf", GradientBoostingClassifier(n_estimators=150, max_depth=3, learning_rate=0.05, subsample=0.8, random_state=42)),
    ])


def usable_rows(df: pd.DataFrame, component: str) -> pd.DataFrame:
    """Rows where this component's own feature columns AND target_up are
    all non-null -- mirrors src/features/engine.py's feature_ready_frame
    pattern (a model must never be fit or scored on a row with a missing
    input, rather than silently imputing)."""
    columns = COMPONENT_COLUMNS[component]
    return df.dropna(subset=[*columns, "target_up"]).reset_index(drop=True)


def fit(component: str, df: pd.DataFrame) -> Pipeline:
    """df must already be usable_rows(df, component)'s output (or an
    equivalent already-clean slice) — this function does not re-check."""
    columns = COMPONENT_COLUMNS[component]
    X = df[columns].to_numpy()
    y = df["target_up"].astype(int).to_numpy()
    pipeline = _build_pipeline()
    pipeline.fit(X, y)
    return pipeline


def predict_proba_up(component: str, pipeline: Pipeline, df: pd.DataFrame) -> np.ndarray:
    columns = COMPONENT_COLUMNS[component]
    X = df[columns].to_numpy()
    return pipeline.predict_proba(X)[:, 1]


# --- META: stacks every component's probability into one calibrated
# output. A simple LogisticRegression, not a GBM -- with typically only 4
# input columns (one probability per component), a GBM would be heavily
# overparameterized; logistic regression over a handful of already-
# informative probabilities is the standard, simpler stacking choice. ---

def fit_meta(component_probs: pd.DataFrame, y: pd.Series) -> LogisticRegression:
    """component_probs: one column per component (e.g. "price",
    "fundamental", "news_event", "market_regime", "cross_market"), each
    already that component's own predict_proba_up output on these exact
    rows. The CALLER is responsible for chronological separation — see
    module docstring."""
    model = LogisticRegression()
    model.fit(component_probs.to_numpy(), y.astype(int).to_numpy())
    return model


def predict_meta(model: LogisticRegression, component_probs: pd.DataFrame) -> np.ndarray:
    return model.predict_proba(component_probs.to_numpy())[:, 1]

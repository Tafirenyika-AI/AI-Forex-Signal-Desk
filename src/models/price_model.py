"""Price/technical component model (blueprint sec. 6, row 1).

v1 baseline per blueprint sec. 14 Phase 1 ("establish simple baselines"):
gradient boosting over the causal technical features, evaluated only with
walk-forward splits (sec. 6.2 — no random shuffling for time series).
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from src.features.engine import FEATURE_COLUMNS


@dataclass(frozen=True)
class WalkForwardSplit:
    train_start: int
    train_end: int  # exclusive
    test_start: int
    test_end: int  # exclusive


def walk_forward_splits(
    n_rows: int,
    train_window: int,
    test_window: int,
    step: int | None = None,
) -> Iterator[WalkForwardSplit]:
    """Rolling-origin splits, strictly time-ordered: every test row comes
    after every row used to train the model that scores it."""
    step = step or test_window
    train_start = 0
    while True:
        train_end = train_start + train_window
        test_end = train_end + test_window
        if test_end > n_rows:
            break
        yield WalkForwardSplit(train_start, train_end, train_end, test_end)
        train_start += step


def build_pipeline() -> Pipeline:
    return Pipeline(
        [
            ("scaler", StandardScaler()),
            (
                "clf",
                GradientBoostingClassifier(
                    n_estimators=150,
                    max_depth=3,
                    learning_rate=0.05,
                    subsample=0.8,
                    random_state=42,
                ),
            ),
        ]
    )


def fit(df: pd.DataFrame) -> Pipeline:
    """df must already have FEATURE_COLUMNS and a non-null target_up column."""
    X = df[FEATURE_COLUMNS].to_numpy()
    y = df["target_up"].astype(int).to_numpy()
    pipeline = build_pipeline()
    pipeline.fit(X, y)
    return pipeline


def predict_proba_up(pipeline: Pipeline, df: pd.DataFrame) -> np.ndarray:
    X = df[FEATURE_COLUMNS].to_numpy()
    return pipeline.predict_proba(X)[:, 1]

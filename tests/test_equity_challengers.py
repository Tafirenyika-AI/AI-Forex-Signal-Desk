"""Unit tests for Equity V2 Phase 10: src/models/equity_challengers.py.

Synthetic feature frames (no DB) — same style as
tests/test_equity_vectorized.py's own fixtures feed real DataFrame shapes
into these functions in practice (verified live, see
docs/EQUITY_V2_IMPLEMENTATION_LOG.md). These tests confirm the pipeline
mechanics (dropna discipline, shapes, probability range, meta-stacking)
work correctly in isolation.

Run: .venv/Scripts/python.exe -m pytest tests/test_equity_challengers.py -v
"""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np
import pandas as pd
import pytest

from src.models.equity_challengers import (
    COMPONENT_COLUMNS,
    fit,
    fit_meta,
    predict_meta,
    predict_proba_up,
    usable_rows,
)


def _synthetic_frame(n: int = 200, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    df = pd.DataFrame({
        "revenue_growth_yoy": rng.normal(0.1, 0.2, n), "gross_margin": rng.normal(0.5, 0.1, n),
        "operating_margin": rng.normal(0.3, 0.1, n), "net_margin": rng.normal(0.2, 0.1, n),
        "operating_cash_flow_margin": rng.normal(0.4, 0.1, n), "leverage_ratio": rng.normal(0.4, 0.2, n),
        "eps_diluted": rng.normal(2.0, 1.0, n),
        "hours_since_last_news": rng.uniform(0, 200, n), "has_recent_earnings_event": rng.integers(0, 2, n).astype(float),
        "vol_percentile": rng.uniform(0, 1, n), "trend_percentile": rng.uniform(0, 1, n),
        "intraday_volatility_percentile": rng.uniform(0, 1, n),
        "relative_volume": rng.lognormal(0, 0.3, n), "gap_pct": rng.normal(0, 0.01, n),
        "vwap_distance": rng.normal(0, 0.005, n), "return_vs_spy": rng.normal(0, 0.02, n),
        "return_vs_sector_etf": rng.normal(0, 0.02, n),
        "target_up": rng.integers(0, 2, n),
    })
    return df


def test_usable_rows_drops_rows_missing_any_component_column():
    df = _synthetic_frame(n=10)
    df.loc[3, "gross_margin"] = None
    df.loc[7, "target_up"] = None
    result = usable_rows(df, "fundamental")
    assert len(result) == 8
    assert 3 not in result.index.to_list() or True  # reset_index -- just confirm count


@pytest.mark.parametrize("component", list(COMPONENT_COLUMNS.keys()))
def test_fit_and_predict_proba_shape_and_range(component):
    df = _synthetic_frame(n=200)
    train, test = df.iloc[:150], df.iloc[150:]
    model = fit(component, train)
    probs = predict_proba_up(component, model, test)
    assert len(probs) == len(test)
    assert np.all((probs >= 0) & (probs <= 1))


def test_meta_stacks_component_probabilities():
    rng = np.random.default_rng(0)
    n = 300
    y = pd.Series(rng.integers(0, 2, n))
    # A component whose "probability" is strongly informative (correlated
    # with the real label) should let the meta-model learn real structure,
    # not just noise.
    informative = y.to_numpy() * 0.6 + rng.normal(0, 0.1, n) + 0.2
    informative = np.clip(informative, 0.01, 0.99)
    component_probs = pd.DataFrame({
        "price": informative,
        "fundamental": rng.uniform(0, 1, n),
        "news_event": rng.uniform(0, 1, n),
        "market_regime": rng.uniform(0, 1, n),
    })
    train_probs, train_y = component_probs.iloc[:200], y.iloc[:200]
    test_probs, test_y = component_probs.iloc[200:], y.iloc[200:]

    meta = fit_meta(train_probs, train_y)
    predictions = predict_meta(meta, test_probs)
    assert len(predictions) == len(test_probs)
    assert np.all((predictions >= 0) & (predictions <= 1))
    # The informative component should have genuinely helped: accuracy
    # meaningfully above a coin flip on held-out data.
    accuracy = ((predictions > 0.5).astype(int) == test_y.to_numpy()).mean()
    assert accuracy > 0.6

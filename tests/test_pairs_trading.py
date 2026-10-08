"""Strategy I (src/strategies/registry.py) — src/strategies/pairs_trading.py.

Tests the real cointegration test (evaluate_cointegration) against synthetic
series with a known-by-construction answer (a genuinely cointegrated pair
vs. two independent random walks), and _evaluate_spread_core against a
fabricated spread series with a known reversion pattern.

Run: .venv/Scripts/python.exe -m pytest tests/test_pairs_trading.py -v
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

from src.strategies.pairs_trading import _evaluate_spread_core, evaluate_cointegration

RNG = np.random.default_rng(42)


def test_a_genuinely_cointegrated_pair_gets_a_low_p_value():
    n = 300
    common = np.cumsum(RNG.normal(0, 0.01, n))
    log_a = pd.Series(common + RNG.normal(0, 0.005, n))
    log_b = pd.Series(common + RNG.normal(0, 0.005, n))
    result = evaluate_cointegration(log_a, log_b)
    assert result.cointegration_p_value is not None
    assert result.cointegration_p_value < 0.01
    assert result.hedge_ratio is not None and result.hedge_ratio > 0


def test_two_independent_random_walks_get_a_high_p_value():
    n = 300
    log_a = pd.Series(np.cumsum(RNG.normal(0, 0.01, n)))
    log_b = pd.Series(np.cumsum(RNG.normal(0, 0.01, n)))
    result = evaluate_cointegration(log_a, log_b)
    assert result.cointegration_p_value is not None
    assert result.cointegration_p_value > 0.1


def test_too_little_history_returns_honest_none_fields():
    log_a = pd.Series([0.1, 0.2, 0.15])
    log_b = pd.Series([0.1, 0.2, 0.15])
    result = evaluate_cointegration(log_a, log_b)
    assert result.cointegration_p_value is None
    assert result.hedge_ratio is None


def test_mean_reverting_spread_shows_a_positive_reversion_signal():
    # A spread that oscillates predictably around 0 -- any stretched
    # reading should be followed by reversion back toward the mean.
    n = 200
    spread = pd.Series([0.1 * np.sin(2 * np.pi * i / 20) for i in range(n)])
    n_result, hit_rate, z_score, mean_move = _evaluate_spread_core(
        spread, spread_lookback=20, entry_z_threshold=1.0, holding_bars=5,
    )
    assert n_result > 0
    assert hit_rate > 0.5


def test_no_stretched_observations_returns_honest_none_fields():
    spread = pd.Series([0.0] * 100)  # perfectly flat, never stretched
    n_result, hit_rate, z_score, mean_move = _evaluate_spread_core(
        spread, spread_lookback=20, entry_z_threshold=2.0, holding_bars=5,
    )
    assert n_result == 0
    assert hit_rate is None and z_score is None and mean_move is None

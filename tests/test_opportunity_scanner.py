"""V4 Priority 4 (brief Section 5) — src/scanner/opportunity_scanner.py.

Tests `rank_from_feature_dicts` directly against fabricated per-ticker
feature dicts (bypassing real build_equity_feature_vector/candle round
trips), same split-core pattern as the Strategy F/C/H modules.

Run: .venv/Scripts/python.exe -m pytest tests/test_opportunity_scanner.py -v
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

from src.scanner.opportunity_scanner import rank_from_feature_dicts

AS_OF = datetime(2026, 10, 8, tzinfo=timezone.utc)


def _vec(log_return_12, return_vs_spy, return_vs_sector_etf, trend_percentile, vol_percentile, relative_volume, vwap_distance):
    return {
        "log_return_12": log_return_12, "return_vs_spy": return_vs_spy,
        "return_vs_sector_etf": return_vs_sector_etf, "trend_percentile": trend_percentile,
        "vol_percentile": vol_percentile, "relative_volume": relative_volume, "vwap_distance": vwap_distance,
    }


def test_strongest_bullish_ticker_ranks_first_and_composite_is_in_range():
    feature_dicts = {
        "STRONG": _vec(0.05, 0.03, 0.02, 0.8, 0.7, 1.5, 0.01),
        "WEAK": _vec(-0.05, -0.03, -0.02, 0.2, 0.1, 0.5, -0.01),
        "MID": _vec(0.0, 0.0, 0.0, 0.5, 0.5, 1.0, 0.0),
    }
    context = {k: {} for k in feature_dicts}
    ranked = rank_from_feature_dicts(feature_dicts, context, AS_OF, timeframe="swing")
    assert ranked[0].ticker == "STRONG"
    assert ranked[0].composite_score == 1.0  # top-ranked in every direction-aware factor, and the only high-magnitude one for volatility/volume
    assert ranked[-1].ticker == "WEAK"
    for r in ranked:
        assert r.composite_score is not None and 0.0 <= r.composite_score <= 1.0


def test_missing_factor_is_excluded_not_defaulted():
    feature_dicts = {
        "A": _vec(0.05, 0.03, None, 0.8, 0.7, 1.5, 0.01),  # no sector ETF (e.g. an ETF or crypto ticker)
        "B": _vec(0.01, 0.01, 0.01, 0.4, 0.3, 1.0, 0.0),
    }
    context = {k: {} for k in feature_dicts}
    ranked = rank_from_feature_dicts(feature_dicts, context, AS_OF, timeframe="swing")
    a = next(r for r in ranked if r.ticker == "A")
    assert "sector_strength" in a.missing_factors
    assert "sector_strength" not in a.factor_scores
    # B still gets ranked on sector_strength since it's the only one with data
    b = next(r for r in ranked if r.ticker == "B")
    assert "sector_strength" in b.factor_scores


def test_intraday_timeframe_uses_different_feature_set_than_swing():
    feature_dicts = {
        "A": {
            "log_return_4": 0.02, "log_return_12": -0.02, "return_vs_spy": 0.0, "return_vs_sector_etf": 0.0,
            "trend_percentile": 0.5, "vol_percentile": 0.5, "intraday_volatility_percentile": 0.9,
            "relative_volume": 1.0, "vwap_distance": 0.0,
        },
    }
    context = {"A": {}}
    intraday = rank_from_feature_dicts(feature_dicts, context, AS_OF, timeframe="intraday")[0]
    swing = rank_from_feature_dicts(feature_dicts, context, AS_OF, timeframe="swing")[0]
    assert "momentum" in intraday.factor_scores and "momentum" in swing.factor_scores
    # Different underlying raw feature (log_return_4 vs log_return_12) --
    # confirmed via the reason text naming the right feature.
    assert "log_return_4" not in swing.reasons[0] or True  # just confirm no crash; real check below
    intraday_momentum_reason = next(r for r in intraday.reasons if r.startswith("momentum"))
    swing_momentum_reason = next(r for r in swing.reasons if r.startswith("momentum"))
    assert "short-horizon" in intraday_momentum_reason
    assert "medium-horizon" in swing_momentum_reason


def test_earnings_context_is_informational_not_part_of_composite():
    feature_dicts = {
        "A": _vec(0.01, 0.01, 0.01, 0.5, 0.5, 1.0, 0.0),
        "B": _vec(0.01, 0.01, 0.01, 0.5, 0.5, 1.0, 0.0),
    }
    context = {"A": {"has_recent_earnings_event": 1.0}, "B": {}}
    ranked = rank_from_feature_dicts(feature_dicts, context, AS_OF, timeframe="swing")
    a = next(r for r in ranked if r.ticker == "A")
    b = next(r for r in ranked if r.ticker == "B")
    assert a.composite_score == b.composite_score  # identical factor inputs -> identical composite
    assert any("earnings" in c for c in a.context)
    assert a.context != b.context

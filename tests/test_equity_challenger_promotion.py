"""Unit tests for Equity V2 Phase 18: src/evaluation/equity_challenger_promotion.py.

Pure-function tests against synthetic WalkForwardModelResult objects.
A separate live check (not a pytest test) was run feeding Phase 11's
real walk-forward results for real NVDA data through this report; see
docs/EQUITY_V2_IMPLEMENTATION_LOG.md.

Run: .venv/Scripts/python.exe -m pytest tests/test_equity_challenger_promotion.py -v
"""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

from src.backtest.equity_walk_forward import WalkForwardModelResult
from src.evaluation.equity_challenger_promotion import build_challenger_promotion_report


def _result(**kwargs) -> WalkForwardModelResult:
    base = dict(
        model_name="x", n_oos_predictions=0, expectancy=0.0, sharpe_like=0.0, sortino_like=0.0,
        profit_factor=1.0, max_drawdown_pct=0.0, tail_loss_ratio=1.0, brier_score=0.25, regime_stability={},
    )
    base.update(kwargs)
    return WalkForwardModelResult(**base)


def test_insufficient_samples_never_passes_and_never_claims_ready():
    challenger = _result(n_oos_predictions=5)
    champion = _result(n_oos_predictions=500)
    report = build_challenger_promotion_report("cross_market", challenger, champion)
    assert report.ready_for_promotion is False
    sample_gate = next(c for c in report.criteria if c.name == "sample_size")
    assert sample_gate.passed is False


def test_genuinely_better_challenger_passes_every_gate():
    challenger = _result(
        n_oos_predictions=200, brier_score=0.20, expectancy=0.002, tail_loss_ratio=-1.0,
        regime_stability={"TREND": {"n": 100, "win_rate": 0.55, "mean_return": 0.001}},
    )
    champion = _result(
        n_oos_predictions=200, brier_score=0.25, expectancy=0.001, tail_loss_ratio=-1.5,
        regime_stability={"TREND": {"n": 100, "win_rate": 0.50, "mean_return": 0.0005}},
    )
    report = build_challenger_promotion_report("cross_market", challenger, champion)
    assert report.ready_for_promotion is True
    assert all(c.passed is True for c in report.criteria)


def test_small_brier_improvement_below_threshold_fails_calibration_gate():
    challenger = _result(n_oos_predictions=200, brier_score=0.249)  # only 0.001 better -- below MIN_BRIER_IMPROVEMENT
    champion = _result(n_oos_predictions=200, brier_score=0.250)
    report = build_challenger_promotion_report("x", challenger, champion)
    calibration_gate = next(c for c in report.criteria if c.name == "calibration")
    assert calibration_gate.passed is False
    assert report.ready_for_promotion is False


def test_a_losing_regime_with_enough_samples_fails_the_regime_gate():
    challenger = _result(
        n_oos_predictions=200, brier_score=0.20, expectancy=0.002,
        regime_stability={
            "TREND": {"n": 100, "win_rate": 0.55, "mean_return": 0.001},
            "RANGE": {"n": 50, "win_rate": 0.40, "mean_return": -0.002},  # genuinely losing, enough samples to trust it
        },
    )
    champion = _result(n_oos_predictions=200, brier_score=0.25, expectancy=0.001)
    report = build_challenger_promotion_report("x", challenger, champion)
    regime_gate = next(c for c in report.criteria if c.name == "regime_stability")
    assert regime_gate.passed is False
    assert report.ready_for_promotion is False


def test_a_losing_regime_with_too_few_samples_does_not_fail_the_gate():
    # A regime with only 3 observations losing money is not yet trustworthy
    # evidence either way -- must not sink an otherwise-clean report on
    # noise this thin.
    challenger = _result(
        n_oos_predictions=200, brier_score=0.20, expectancy=0.002, tail_loss_ratio=-1.0,
        regime_stability={
            "TREND": {"n": 197, "win_rate": 0.55, "mean_return": 0.001},
            "SHOCK": {"n": 3, "win_rate": 0.0, "mean_return": -0.05},  # real but far too thin to judge
        },
    )
    champion = _result(n_oos_predictions=200, brier_score=0.25, expectancy=0.001, tail_loss_ratio=-1.5)
    report = build_challenger_promotion_report("x", challenger, champion)
    regime_gate = next(c for c in report.criteria if c.name == "regime_stability")
    assert regime_gate.passed is True


def test_worse_tail_loss_ratio_fails_its_own_gate():
    challenger = _result(n_oos_predictions=200, brier_score=0.20, expectancy=0.002, tail_loss_ratio=-3.0)
    champion = _result(n_oos_predictions=200, brier_score=0.25, expectancy=0.001, tail_loss_ratio=-1.0)
    report = build_challenger_promotion_report("x", challenger, champion)
    tail_gate = next(c for c in report.criteria if c.name == "tail_loss")
    assert tail_gate.passed is False


def test_nan_tail_loss_ratio_is_insufficient_evidence_not_a_failure():
    challenger = _result(n_oos_predictions=200, tail_loss_ratio=float("nan"))
    champion = _result(n_oos_predictions=200, tail_loss_ratio=float("nan"))
    report = build_challenger_promotion_report("x", challenger, champion)
    tail_gate = next(c for c in report.criteria if c.name == "tail_loss")
    assert tail_gate.passed is None


def test_report_never_has_a_promote_function_only_reports():
    # Structural assertion, not behavioral: confirms this module's own
    # absolute constraint (see its docstring) -- no promote()/write path
    # exists to accidentally call.
    import src.evaluation.equity_challenger_promotion as module
    assert not hasattr(module, "promote")
    assert not any(name.startswith("apply_") for name in dir(module))

"""Unit tests for P1-02 (external review, 2026-09-22): the OANDA-only
meta-model must never be applied to non-forex decisions, and a meta-model
veto must be explained honestly (not as "didn't clear the threshold" when
it did).

Run: .venv/Scripts/python.exe -m pytest tests/test_meta_model_scope.py -v
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

from src.broker.registry import asset_class_for
from src.decision.fusion import ComponentView, fuse
from src.models.train_meta_model import (
    AUTO_DEPLOY_MIN_ACCURACY,
    AUTO_DEPLOY_MIN_MARGIN_OVER_BASELINE,
    required_accuracy_for_auto_deploy,
)


class _FakeModel:
    def __init__(self, p_win: float):
        self._p_win = p_win

    def predict_proba(self, features):
        return [[1 - self._p_win, self._p_win]]


def _strong_buy_views():
    # Engineered to clear the action threshold on its own (price component
    # alone, high score+confidence, no disagreement).
    return [ComponentView("price", 0.9, 0.9)]


# --- asset-class gating (the routing bug itself) ---

def test_asset_class_for_correctly_distinguishes_affected_instruments():
    # The exact instruments confirmed live-affected by this bug.
    for forex_pair in ["EUR_USD", "USD_JPY", "GBP_USD"]:
        assert asset_class_for(forex_pair) == "forex"
    for non_forex in ["NVDA", "AAPL", "MSFT", "QQQ", "BTC/USD", "ETH/USD"]:
        assert asset_class_for(non_forex) != "forex"


def test_fuse_with_meta_model_none_never_applies_it():
    # This is what run_loop.py now passes for any non-forex instrument
    # (meta_model_for_instrument = meta_model if asset_class_for(pair) ==
    # "forex" else None) -- confirms the heuristic action/confidence
    # stand untouched when meta_model is None, regardless of instrument.
    decision = fuse(
        instrument="NVDA", horizon="1h", regime="TREND",
        component_views=_strong_buy_views(), current_price=100.0, atr_14=1.0,
        data_freshness={}, now=datetime(2026, 9, 23, tzinfo=timezone.utc),
        meta_model=None,
    )
    assert decision.action == "BUY"
    assert "meta-model" not in decision.explanation


# --- honest veto explanation (the misleading-text bug found while verifying the above) ---

def test_meta_model_veto_explanation_states_the_real_reason():
    # Real bug found live in production (2026-09-22): a real NVDA signal
    # with combined_score=+0.528 (well past the +-0.200 threshold) was
    # explained as "did not clear the action threshold" -- false; the
    # real reason was a meta-model veto. Reproduced synthetically here.
    feature_cols = ["price_agreement", "price_confidence"]
    meta_model = (_FakeModel(p_win=0.17), feature_cols)

    decision = fuse(
        instrument="EUR_USD", horizon="1h", regime="TREND",
        component_views=_strong_buy_views(), current_price=1.08, atr_14=0.001,
        data_freshness={}, now=datetime(2026, 9, 23, tzinfo=timezone.utc),
        meta_model=meta_model,
    )
    assert decision.action == "NO_TRADE"
    assert "vetoed" in decision.explanation
    assert "cleared" in decision.explanation  # honest: says it DID clear
    assert "did not clear the" not in decision.explanation  # the bug's false claim


def test_genuine_threshold_miss_explanation_unchanged():
    # A real "the heuristic itself never wanted to trade" NO_TRADE (score
    # too small) must keep its original, correct explanation -- no meta-
    # model involved, or one that never got the chance to veto anything.
    weak_views = [ComponentView("price", 0.05, 0.3)]  # well under the 0.2 threshold
    decision = fuse(
        instrument="EUR_USD", horizon="1h", regime="TREND",
        component_views=weak_views, current_price=1.08, atr_14=0.001,
        data_freshness={}, now=datetime(2026, 9, 23, tzinfo=timezone.utc),
        meta_model=None,
    )
    assert decision.action == "NO_TRADE"
    assert "did not clear the" in decision.explanation
    assert "vetoed" not in decision.explanation


# --- promotion floor beats the majority-class baseline, not just 50% ---

def test_real_world_case_no_longer_auto_deploys():
    # The exact live case that prompted this fix: 36 samples (10 win, 26
    # loss) -> majority-class baseline 26/36 = 72.22%; the deployed model
    # scored 72.14% cv-accuracy -- BELOW its own baseline, let alone a
    # meaningful margin above it. Confirms it would not clear the new bar.
    baseline_accuracy = 26 / 36
    required = required_accuracy_for_auto_deploy(baseline_accuracy)
    cv_accuracy_mean = 0.7214285714285715
    assert cv_accuracy_mean < required


def test_balanced_classes_only_need_the_flat_floor():
    # 50/50 balance -> baseline is 50%, so the margin-over-baseline
    # (55%) is now the binding constraint, not the flat floor -- still
    # higher than the old flat 50% floor alone would have required.
    required = required_accuracy_for_auto_deploy(0.5)
    assert required == AUTO_DEPLOY_MIN_ACCURACY + AUTO_DEPLOY_MIN_MARGIN_OVER_BASELINE


def test_a_model_that_genuinely_beats_its_baseline_still_auto_deploys():
    # A model meaningfully better than trivial guessing must still clear
    # the new bar -- this isn't a de facto ban on auto-deploy.
    baseline_accuracy = 0.55
    required = required_accuracy_for_auto_deploy(baseline_accuracy)
    genuinely_good_cv_accuracy = 0.75
    assert genuinely_good_cv_accuracy > required


def test_extreme_imbalance_still_respects_the_absolute_floor():
    # A 95%-one-sided sample's baseline+margin (100%) would be
    # impossible to clear -- max() with the flat floor doesn't help here
    # since baseline already dominates, which is the CORRECT conservative
    # behavior (an extremely imbalanced small sample shouldn't auto-
    # deploy on this metric at all, by design).
    required = required_accuracy_for_auto_deploy(0.95)
    assert required == 0.95 + AUTO_DEPLOY_MIN_MARGIN_OVER_BASELINE


def test_meta_model_confirms_rather_than_vetoes_gets_normal_explanation():
    # When the meta-model AGREES (p_win above the floor), the final action
    # is BUY/SELL, not NO_TRADE -- exercises the untouched else-branch.
    feature_cols = ["price_agreement", "price_confidence"]
    meta_model = (_FakeModel(p_win=0.8), feature_cols)

    decision = fuse(
        instrument="EUR_USD", horizon="1h", regime="TREND",
        component_views=_strong_buy_views(), current_price=1.08, atr_14=0.001,
        data_freshness={}, now=datetime(2026, 9, 23, tzinfo=timezone.utc),
        meta_model=meta_model,
    )
    assert decision.action == "BUY"
    assert "meta-model's, not the heuristic's" in decision.explanation

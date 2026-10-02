"""Equity V2 Phase 18 — champion/challenger promotion gate.

Formalizes whether a Phase 10 shadow challenger has earned promotion,
using Phase 11's real walk-forward out-of-sample results — never
training-set performance, never automatic. Reuses
src/evaluation/promotion_gates.py's own GateCriterion/PromotionGateReport
shape (that module's own job is the overall SYSTEM deployment phase, A
through G; this one is the narrower, per-MODEL question of "has this
specific challenger earned the right to be considered for promotion
alongside the existing price-model champion" — same reporting
philosophy, same honest "not enough evidence yet" posture, different
subject).

Absolute, non-negotiable: **this module only ever REPORTS a recommendation
— it has no promote() function, no write path, and is never called by
anything in src/run_loop.py, src/decision/fusion.py, or src/models/
promote_meta_model.py.** A human reads this report and decides, exactly
as the brief's own Phase 18 instruction requires ("no automatic self-
modification of production models... promotion requires explicit
chronological out-of-sample evidence, preferably human approval"). Even
if every criterion below shows green, `ready_for_promotion=True` means
"the evidence supports considering promotion," never "promoted."

Challengers remain shadow-only regardless of this report's outcome
(Phase 10's own module docstring) — nothing about running this report,
or even a clean pass, changes that. Actually wiring a promoted
challenger into the live decision path (src/decision/fusion.py's
COMPONENT_WEIGHTS, or a successor to it for equities) would be a
separate, deliberate, human-reviewed code change, not a consequence of
this report.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from src.backtest.equity_walk_forward import WalkForwardModelResult
from src.evaluation.promotion_gates import MIN_SAMPLES_FOR_GATE, GateCriterion

# A challenger's Brier score must beat the champion's by at least this
# much to count as genuinely better, not noise -- same philosophy as
# src/models/calibration.py's own 0.25 constant-guess baseline: a tiny
# improvement within measurement noise shouldn't flip a promotion
# decision. Not derived from a formal significance test (this project's
# sample sizes don't yet support one) -- a deliberately conservative,
# disclosed threshold.
MIN_BRIER_IMPROVEMENT = 0.01

# A challenger must not show a NEGATIVE mean return in any regime the
# champion itself trades acceptably in -- a challenger that's better on
# average but quietly loses badly in one real regime is not a clean win,
# it's a different, undiagnosed risk profile a human needs to see called
# out explicitly, not buried in an aggregate number.
MAX_ACCEPTABLE_LOSING_REGIMES = 0


@dataclass(frozen=True)
class ChallengerPromotionReport:
    challenger_name: str
    champion_name: str
    criteria: list[GateCriterion]
    ready_for_promotion: bool  # "the evidence supports considering promotion" -- NEVER "promoted"; see module docstring


def _sample_size_criterion(result: WalkForwardModelResult) -> GateCriterion:
    passed = result.n_oos_predictions >= MIN_SAMPLES_FOR_GATE
    return GateCriterion(
        "sample_size", f"At least {MIN_SAMPLES_FOR_GATE} genuine out-of-sample predictions",
        f"{result.n_oos_predictions} OOS predictions", passed if result.n_oos_predictions > 0 else None,
    )


def _calibration_criterion(challenger: WalkForwardModelResult, champion: WalkForwardModelResult) -> GateCriterion:
    if challenger.n_oos_predictions < MIN_SAMPLES_FOR_GATE or champion.n_oos_predictions < MIN_SAMPLES_FOR_GATE:
        return GateCriterion(
            "calibration", f"Challenger's Brier score beats champion's by >= {MIN_BRIER_IMPROVEMENT}",
            "insufficient samples for a stable read", None,
        )
    improvement = champion.brier_score - challenger.brier_score  # lower Brier is better
    passed = improvement >= MIN_BRIER_IMPROVEMENT
    return GateCriterion(
        "calibration", f"Challenger's Brier score beats champion's by >= {MIN_BRIER_IMPROVEMENT}",
        f"challenger={challenger.brier_score:.3f} vs champion={champion.brier_score:.3f} (delta {improvement:+.3f})", passed,
    )


def _expectancy_criterion(challenger: WalkForwardModelResult, champion: WalkForwardModelResult) -> GateCriterion:
    if challenger.n_oos_predictions < MIN_SAMPLES_FOR_GATE:
        return GateCriterion(
            "expectancy", "Challenger's expectancy is not worse than the champion's",
            "insufficient samples for a stable read", None,
        )
    passed = challenger.expectancy >= champion.expectancy
    return GateCriterion(
        "expectancy", "Challenger's expectancy is not worse than the champion's",
        f"challenger={challenger.expectancy:+.5f} vs champion={champion.expectancy:+.5f}", passed,
    )


def _regime_stability_criterion(challenger: WalkForwardModelResult) -> GateCriterion:
    if not challenger.regime_stability:
        return GateCriterion(
            "regime_stability", f"No more than {MAX_ACCEPTABLE_LOSING_REGIMES} regime(s) with a net-negative mean return",
            "no regime breakdown available", None,
        )
    losing_regimes = [
        regime for regime, stats in challenger.regime_stability.items()
        if stats["n"] >= MIN_SAMPLES_FOR_GATE and stats["mean_return"] < 0
    ]
    passed = len(losing_regimes) <= MAX_ACCEPTABLE_LOSING_REGIMES
    detail = f"losing regime(s) with enough samples to judge: {losing_regimes}" if losing_regimes else "no regime loses money on average"
    return GateCriterion(
        "regime_stability", f"No more than {MAX_ACCEPTABLE_LOSING_REGIMES} regime(s) with a net-negative mean return",
        detail, passed,
    )


def _tail_loss_criterion(challenger: WalkForwardModelResult, champion: WalkForwardModelResult) -> GateCriterion:
    if challenger.n_oos_predictions < MIN_SAMPLES_FOR_GATE:
        return GateCriterion(
            "tail_loss", "Challenger's worst-case loss ratio is not meaningfully worse than the champion's",
            "insufficient samples for a stable read", None,
        )
    if math.isnan(challenger.tail_loss_ratio) or math.isnan(champion.tail_loss_ratio):
        return GateCriterion(
            "tail_loss", "Challenger's worst-case loss ratio is not meaningfully worse than the champion's",
            "no losing predictions to measure a tail from yet", None,
        )
    passed = challenger.tail_loss_ratio >= champion.tail_loss_ratio  # less negative (or equal) is better/no-worse
    return GateCriterion(
        "tail_loss", "Challenger's worst-case loss ratio is not meaningfully worse than the champion's",
        f"challenger={challenger.tail_loss_ratio:.2f}x vs champion={champion.tail_loss_ratio:.2f}x", passed,
    )


def build_challenger_promotion_report(
    challenger_name: str, challenger: WalkForwardModelResult, champion: WalkForwardModelResult,
) -> ChallengerPromotionReport:
    criteria = [
        _sample_size_criterion(challenger),
        _calibration_criterion(challenger, champion),
        _expectancy_criterion(challenger, champion),
        _regime_stability_criterion(challenger),
        _tail_loss_criterion(challenger, champion),
    ]
    # Never True on missing evidence (None) -- same strict rule
    # src/evaluation/promotion_gates.py's own build_report already
    # enforces for the system-level phase gate.
    ready = all(c.passed for c in criteria)
    return ChallengerPromotionReport(
        challenger_name=challenger_name, champion_name="price",
        criteria=criteria, ready_for_promotion=ready,
    )

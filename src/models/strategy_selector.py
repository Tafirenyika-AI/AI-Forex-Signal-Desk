"""AI Trading Desk V4 Priority 5 (brief Section 10 — "Adaptive Meta-Model").

A strategy-selection engine: given a candidate instrument and a point in
time, decides BUY/SELL/NO_TRADE and, if not NO_TRADE, which strategy's
live signal justified it — NOT a blend of component scores (that's
src/decision/fusion.py's existing, different job), a genuine SELECTION
among named strategy families, per the brief's own distinction
(docs/V4_ARCHITECTURE.md's gap-analysis row 10: "V4 wants real learned
strategy SELECTION (which strategy, not just how to blend scores)").

"Do not use arbitrary confidence scores as proof of profitability" (the
brief's own Section 10 instruction) is honored structurally here, not just
by policy: only strategies with REAL, recorded, statistically significant
supporting evidence from docs/V4_STRATEGY_RESEARCH.md are eligible
candidates at all (see `ELIGIBLE_STRATEGIES` below) — a strategy whose own
hypothesis test came back null (Strategy C) or was actively CONTRADICTED
(Strategy H) is excluded from selection, not merely down-weighted, until
real new evidence changes that.

**ELIGIBLE_STRATEGIES = () as of 2026-10-08 (Priority 6 finding) — Strategy
A's own eligibility was PULLED, not just disclosed.** This module originally
shipped with `ELIGIBLE_STRATEGIES = ("A",)` based solely on Strategy A's
sign-prediction hit-rate (docs/V4_STRATEGY_RESEARCH.md Section 3). A
follow-up real portfolio backtest (Priority 6, same day) of 129 real
signals generated from that exact sign-prediction, using standard ATR
stop/target sizing, came back UNPROFITABLE (net return -4.08%, profit
factor 0.88, Sharpe -0.39) — correctly predicting a return's sign more
often than chance does not by itself produce a profitable trading rule.
Selecting on the hit-rate evidence alone, now that a real backtest
contradicts it, would be exactly the "arbitrary confidence" the brief
warns against. Re-adding "A" (or anything else) requires a real,
profitable `BACKTESTED`-stage result, not a reversion to the earlier,
now-superseded hit-rate-only bar.

"NO_TRADE must be a normal and acceptable decision" — with zero
currently-eligible strategies, NO_TRADE is presently the ONLY possible
outcome of this selector, which is itself the correct, honest reflection
of the real evidence on hand today, not a bug.

"The meta-model must not override the independent risk governor" — this
module makes no broker call, touches no risk_decisions/trade_intents
table, and is never called from run_loop.py's live cycle in this pass; its
output is a `StrategySelection` record for shadow/research use only
(V4_SHADOW_ONLY is true by default, src/v4/feature_flags.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.engine import Engine

from src.strategies.time_series_momentum import current_momentum_signal

MODEL_VERSION = "v4-priority5-selector-2026-10-08"

# Only strategies with real, recorded, statistically significant AND
# PROFITABLE-IN-BACKTEST evidence (docs/V4_STRATEGY_RESEARCH.md) are
# eligible to be SELECTED -- not every HYPOTHESIS_TESTED strategy, and not
# a hit-rate finding alone. Strategy A cleared z>2.5 across all 3
# instruments tested at its validated 84-day config, BUT a real portfolio
# backtest of that exact signal came back unprofitable (see this module's
# own docstring) -- pulled, not merely never added. Strategy C showed no
# significant signal; Strategy F's one significant result (AAPL, z=2.12)
# was flagged too small a sample (n=8) to trust; Strategy H's result
# actively CONTRADICTED its own hypothesis. Every entry here requires both
# a significant hypothesis-test result AND a profitable backtest result --
# neither alone is sufficient.
ELIGIBLE_STRATEGIES: tuple[str, ...] = ()

# The exact configuration docs/V4_STRATEGY_RESEARCH.md's Section 3 found
# real evidence for (NVDA/AAPL/MSFT, z=2.55/3.34/6.32) -- selection must
# use what was actually validated, not a different, untested lookback.
_STRATEGY_A_VALIDATED_INSTRUMENTS: tuple[str, ...] = ("NVDA", "AAPL", "MSFT")
_STRATEGY_A_LOOKBACK_DAYS = 84


@dataclass(frozen=True)
class StrategySelection:
    candidate_symbol: str
    action: str  # BUY / SELL / NO_TRADE
    strategy_selected: str | None  # a STRATEGY_REGISTRY code, or None for NO_TRADE
    timeframe: str
    estimated_net_advantage: float | None  # research-grade only -- the validated strategy's own mean_move_in_favor, NOT a live cost-adjusted estimate
    confidence: float | None  # research-grade: the validated hit_rate for the strategy/instrument that fired, NOT a calibrated probability
    supporting_evidence: str
    risk_assessment: str
    model_version: str


def select_strategy(
    engine: Engine, instrument: str, as_of: datetime, broker: str = "alpaca", granularity: str = "H4",
) -> StrategySelection:
    risk_assessment = (
        "This selection has NOT passed through the live risk governor (src/risk/governor.py) and "
        "carries no size/stop recommendation -- it is a shadow-only research output, never a broker-"
        "ready order. V4_SHADOW_ONLY (src/v4/feature_flags.py) must stay true until a real promotion "
        "decision is made (brief Section 17, champion/challenger)."
    )

    if "A" not in ELIGIBLE_STRATEGIES:
        return StrategySelection(
            candidate_symbol=instrument, action="NO_TRADE", strategy_selected=None, timeframe="swing",
            estimated_net_advantage=None, confidence=None,
            supporting_evidence=(
                "Strategy A's selector eligibility was pulled 2026-10-08: its sign-prediction hit-rate is "
                "real, but a real portfolio backtest of that exact signal came back unprofitable (net return "
                "-4.08%, profit factor 0.88, Sharpe -0.39 — docs/V4_STRATEGY_RESEARCH.md Section 3's own "
                "follow-up finding). No strategy currently meets both the hypothesis-test AND profitable-"
                "backtest bar this selector requires — NO_TRADE is the correct, honest reflection of that, "
                "not a gap in coverage."
            ),
            risk_assessment=risk_assessment, model_version=MODEL_VERSION,
        )

    if instrument not in _STRATEGY_A_VALIDATED_INSTRUMENTS:
        return StrategySelection(
            candidate_symbol=instrument, action="NO_TRADE", strategy_selected=None, timeframe="swing",
            estimated_net_advantage=None, confidence=None,
            supporting_evidence=(
                f"No eligible strategy has validated evidence for {instrument} — only "
                f"{', '.join(_STRATEGY_A_VALIDATED_INSTRUMENTS)} have been tested (docs/V4_STRATEGY_RESEARCH.md "
                "Section 3). Selecting here would be exactly the 'arbitrary confidence' the brief warns against."
            ),
            risk_assessment=risk_assessment, model_version=MODEL_VERSION,
        )

    signal = current_momentum_signal(
        engine, broker, instrument, granularity, lookback_days=_STRATEGY_A_LOOKBACK_DAYS,
    )
    if signal.direction is None:
        return StrategySelection(
            candidate_symbol=instrument, action="NO_TRADE", strategy_selected=None, timeframe="swing",
            estimated_net_advantage=None, confidence=None,
            supporting_evidence=(
                "Strategy A's live signal did not fire (no trailing return, or insufficient history as of "
                f"{as_of.isoformat()}) — NO_TRADE is the normal, expected outcome here, not a failure."
            ),
            risk_assessment=risk_assessment, model_version=MODEL_VERSION,
        )

    action = "BUY" if signal.direction > 0 else "SELL"
    # Research-grade estimates from docs/V4_STRATEGY_RESEARCH.md's own
    # Section 3 table, per instrument, at the validated 84d/30d config --
    # NOT recomputed live (that would re-run the exact test this
    # selection is supposed to be ACTING on the result of, and would be
    # slow to call on every selection). A real production version would
    # cache/refresh these periodically as a scheduled job, not inline here.
    _validated_hit_rate_and_move = {
        "NVDA": (0.535, 0.00196), "AAPL": (0.547, 0.00912), "MSFT": (0.588, 0.00023),
    }
    hit_rate, mean_move = _validated_hit_rate_and_move[instrument]

    return StrategySelection(
        candidate_symbol=instrument, action=action, strategy_selected="A", timeframe="swing",
        estimated_net_advantage=mean_move, confidence=hit_rate,
        supporting_evidence=(
            f"Strategy A (time-series momentum): current 84-day trailing return "
            f"{signal.trailing_return:+.4f} as of {signal.as_of} — direction matches the validated "
            f"finding for {instrument} (hit_rate={hit_rate:.1%}, docs/V4_STRATEGY_RESEARCH.md Section 3). "
            "Confidence/advantage figures are this strategy's own historical validation result, not a "
            "live-recalibrated probability — disclosed explicitly, not presented as more certain than it is."
        ),
        risk_assessment=risk_assessment, model_version=MODEL_VERSION,
    )

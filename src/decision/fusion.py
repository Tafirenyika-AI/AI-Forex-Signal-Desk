"""Decision Fusion (blueprint sec. 3 layer 7, sec. 5.2, sec. 6 meta-model row).

Honesty note on the meta-model: the blueprint's meta-model row calls for a
"Logistic/GBM/calibration layer trained only on out-of-sample component
outputs." That requires enough historical logged predictions-vs-outcomes
across price/macro/news components together to fit and validate — which
doesn't exist yet on day one. v1 here is a documented, fixed-weight
heuristic combiner, not a trained, calibrated model. `confidence` on the
resulting decision is therefore a heuristic strength score in [0, 1], not a
calibrated probability — don't let downstream code treat it as one until
model_registry has a validated meta-model logged against real history.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# Fixed v1 weights. Price is the only component with a walk-forward backtest
# behind it (src/backtest/engine.py). cross_market rides on clean, always-
# fresh FRED data (yields/dollar-index/oil) even though its rules are simple,
# so it gets real weight.
#
# news was weighted zero on 2026-08-12: Alpha Vantage's free tier reported
# exactly 0.000 confidence across 365 logged predictions (see git history /
# project memory), pure dilution at any positive weight. Restored to 10% on
# 2026-08-13 after GDELT (src/news/gdelt.py) replaced it as the primary
# news source — measured confidence with real live GDELT data came back
# 0.08-0.17 across all five pairs, real and non-zero, unlike Alpha Vantage's
# permanent zero. Modest weight matches modest measured confidence; revisit
# if GDELT's real contribution proves stronger or weaker over more cycles.
#
# session added 2026-08-13 (user-requested — src/models/trading_sessions.py):
# where price sits relative to the prior session's real range at a session
# handoff (London->New York etc.), self-relative to that session's own
# range size, with confidence deliberately damped (not boosted) right at a
# session's own open — that's precisely the window where a new session is
# most likely to reverse/correct the prior one rather than confirm it, so
# less certainty is the honest read until the new session has had time to
# actually confirm a direction. Modest starting weight, same posture as
# news's own introduction above — real live signal, not yet battle-tested
# over many cycles.
COMPONENT_WEIGHTS = {
    "price": 0.50,
    "macro": 0.20,
    "cross_market": 0.20,
    "news": 0.10,
    "session": 0.10,
}

# Spec sec. 16's freshness-confidence check needs its own thresholds,
# deliberately not imported from src/risk/governor.py's
# FRESHNESS_THRESHOLDS_SECONDS — that gate is a hard, binary pass/fail by
# design (risk gates are meant to be simple/deterministic, see that
# module's own docstring), while this is a softer, earlier warning zone:
# confidence starts tapering here, well before a feed is stale enough for
# the risk governor to reject the trade outright. Decision and risk stay
# independent either way — this file never imports risk/governor.py.
FRESHNESS_SOFT_THRESHOLDS_SECONDS = {
    "price": 45.0,
    "candles": 3600.0,
}

ACTION_THRESHOLD = 0.20  # |combined_score| must clear this for BUY/SELL
ATR_STOP_MULTIPLIER = 1.5
REWARD_RISK_MULTIPLE = 1.5

# Autonomous Upgrade Spec sec. 11 "Market Regime Router": one fixed weight
# set should not govern every environment — route weighting by the regime
# the pair is currently in. Only mapped for regimes this system can actually
# detect (src/models/regime.py: TREND/RANGE/SHOCK/HIGH_VOLATILITY/UNKNOWN via
# trailing percentile of vol/trend + a shock-return flag). The spec names
# three more regimes this system has no dedicated detector for yet
# (low-liquidity, risk-on/off, central-bank-event) — not silently
# approximated here; central-bank events already get their own hard lockout
# gate in the risk governor's event check, and low-liquidity is already
# covered by the spread gate, so those two are handled elsewhere in the
# pipeline even without a named "regime" for them. Risk-on/risk-off has no
# equivalent anywhere yet — genuinely missing, left for a future priority.
#
# TREND: price is the only component with real momentum/trend information
# (it's the only one with a walk-forward backtest — see COMPONENT_WEIGHTS
# comment), so trending environments lean into it and lean off components
# that reason about relative/structural levels. RANGE inverts that: no
# trend to ride, so structural/macro/cross-asset context matters more
# relative to a momentum signal that has nothing to grip. SHOCK ("news
# shock" in spec language — the closest detector this system has is a
# large-return-vs-trailing-vol flag, not a literal news-velocity spike, see
# src/models/regime.py) suppresses the technical/momentum read the spec
# calls for, since a shock invalidates recent price structure; only leans
# toward macro since macro data isn't invalidated by a single price spike.
# UNKNOWN dampens every component uniformly — spec: "default toward no
# trade" — achieved here via REGIME_THRESHOLD_MULTIPLIERS instead of zeroing
# weights outright, so it still gets logged with real (if damped) scores.
REGIME_WEIGHT_MULTIPLIERS: dict[str, dict[str, float]] = {
    "TREND": {"price": 1.3, "macro": 0.9, "cross_market": 0.9, "news": 0.8},
    "RANGE": {"price": 0.7, "macro": 1.15, "cross_market": 1.15, "news": 1.1},
    "SHOCK": {"price": 0.4, "macro": 1.1, "cross_market": 0.7, "news": 0.6},
    "HIGH_VOLATILITY": {"price": 1.0, "macro": 1.0, "cross_market": 1.0, "news": 1.0},
    "UNKNOWN": {"price": 1.0, "macro": 1.0, "cross_market": 1.0, "news": 1.0},
}

# Spec sec. 11: high volatility -> "increase minimum confidence"; unknown/
# conflicting -> "default toward no trade". Expressed as a multiplier on
# ACTION_THRESHOLD (not a new fixed constant) so it stays proportional if
# ACTION_THRESHOLD itself is ever retuned. Sizing is reduced separately, in
# the risk governor (src/risk/governor.py REGIME_SIZE_MULTIPLIERS) — spec's
# "reduce size" is a position-sizing concern, not a fusion-layer one, and
# the risk governor is deliberately the only place allowed to shrink size
# (see that module's own docstring).
REGIME_THRESHOLD_MULTIPLIERS: dict[str, float] = {
    "HIGH_VOLATILITY": 1.5,
    "SHOCK": 2.0,
    "UNKNOWN": 1.5,
}

# Once a real meta-model is trained and deployed (src/models/train_meta_model.py,
# src/models/promote_meta_model.py), its calibrated P(win) must clear this to
# keep the heuristic's chosen direction — below it, the meta-model vetoes to
# NO_TRADE even if the heuristic liked the trade. The meta-model can only
# confirm/veto the direction the heuristic already picked, never choose a
# different one — it's trained on component agreement with an already-chosen
# direction (see train_meta_model.py's docstring), not on picking direction.
META_MODEL_MIN_WIN_PROB = 0.50


@dataclass(frozen=True)
class ComponentView:
    name: str
    score: float  # -1 (bearish) .. +1 (bullish)
    confidence: float  # 0..1, how much to trust `score`
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TradeDecision:
    instrument: str
    time: datetime
    action: str  # BUY / SELL / NO_TRADE
    confidence: float
    horizon: str
    regime: str
    key_drivers: list[str]
    contrary_evidence: list[str]
    entry_condition: str
    invalidation: str
    stop_distance: float | None
    take_profit_distance: float | None
    target_logic: str
    data_freshness: dict[str, float]
    explanation: str
    component_views: list[ComponentView]


def price_component_view(p_up: float, backtest_hit_rate: float | None = None) -> ComponentView:
    score = 2 * p_up - 1
    confidence = abs(score)
    detail = {"p_up": p_up}
    if backtest_hit_rate is not None:
        detail["backtest_hit_rate"] = backtest_hit_rate
    return ComponentView("price", score, confidence, detail)


def _apply_meta_model(
    action: str, component_views: list[ComponentView], meta_model: tuple[Any, list[str]]
) -> tuple[str, float | None, str | None]:
    """Recalibrates (or vetoes) a heuristic-chosen action using a trained
    meta-model. Returns (action, calibrated_confidence_or_None, note_or_None)
    — confidence/note are None when the model can't be applied (e.g. a
    component it needs is missing), in which case the heuristic's own
    confidence stands untouched rather than guessing."""
    if action == "NO_TRADE":
        return action, None, None

    model, feature_cols = meta_model
    direction = 1 if action == "BUY" else -1
    by_name = {v.name: v for v in component_views}

    features = []
    for col in feature_cols:
        component, kind = col.rsplit("_", 1)
        view = by_name.get(component)
        if view is None:
            return action, None, None
        features.append(view.score * direction if kind == "agreement" else view.confidence)

    p_win = float(model.predict_proba([features])[0][1])
    if p_win < META_MODEL_MIN_WIN_PROB:
        return "NO_TRADE", None, f"meta-model calibrated P(win)={p_win:.2f} below {META_MODEL_MIN_WIN_PROB:.0%} — vetoed"
    return action, p_win, f"meta-model calibrated P(win)={p_win:.2f}"


def fuse(
    instrument: str,
    horizon: str,
    regime: str,
    component_views: list[ComponentView],
    current_price: float,
    atr_14: float,
    data_freshness: dict[str, float],
    now: datetime | None = None,
    weights: dict[str, float] = COMPONENT_WEIGHTS,
    meta_model: tuple[Any, list[str]] | None = None,
) -> TradeDecision:
    now = now or datetime.now(timezone.utc)

    regime_multipliers = REGIME_WEIGHT_MULTIPLIERS.get(regime, {})
    effective_weights = {
        name: w * regime_multipliers.get(name, 1.0) for name, w in weights.items()
    }

    weighted = [(effective_weights.get(v.name, 0.0), v) for v in component_views]
    total_weight = sum(w for w, _ in weighted)
    numerator = sum(w * v.score * v.confidence for w, v in weighted)
    denominator = sum(w * v.confidence for w, v in weighted)
    combined_score = numerator / denominator if denominator > 0 else 0.0
    combined_confidence = min(1.0, abs(combined_score) * (denominator / total_weight)) if total_weight else 0.0

    # Spec sec. 16: "prevent very high confidence when source disagreement
    # or data freshness is poor." Disagreement is already dampened for free
    # by the formula above (opposite-signed component scores partially
    # cancel in the numerator, shrinking combined_score, hence confidence,
    # before this line even runs) — freshness needed an explicit check.
    freshness_factor = 1.0
    for feed, age in data_freshness.items():
        soft_threshold = FRESHNESS_SOFT_THRESHOLDS_SECONDS.get(feed)
        if soft_threshold and age > soft_threshold:
            overage_ratio = min(1.0, (age - soft_threshold) / soft_threshold)
            freshness_factor = min(freshness_factor, 1.0 - 0.5 * overage_ratio)
    combined_confidence *= freshness_factor

    effective_threshold = ACTION_THRESHOLD * REGIME_THRESHOLD_MULTIPLIERS.get(regime, 1.0)
    if combined_score >= effective_threshold:
        action = "BUY"
    elif combined_score <= -effective_threshold:
        action = "SELL"
    else:
        action = "NO_TRADE"

    meta_note = None
    if meta_model is not None:
        action, meta_confidence, meta_note = _apply_meta_model(action, component_views, meta_model)
        if meta_confidence is not None:
            combined_confidence = meta_confidence

    key_drivers = []
    contrary_evidence = []
    for w, v in weighted:
        tag = f"{v.name}: score={v.score:+.2f} conf={v.confidence:.2f}"
        if action == "BUY" and v.score < 0:
            contrary_evidence.append(tag)
        elif action == "SELL" and v.score > 0:
            contrary_evidence.append(tag)
        else:
            key_drivers.append(tag)

    if regime == "SHOCK":
        contrary_evidence.append("regime=SHOCK: elevated slippage/gap risk")
    if regime == "UNKNOWN":
        contrary_evidence.append("regime=UNKNOWN: insufficient trailing history to classify")

    stop_distance = ATR_STOP_MULTIPLIER * atr_14 if atr_14 else None
    take_profit_distance = (
        REWARD_RISK_MULTIPLE * stop_distance if stop_distance is not None else None
    )

    if action == "NO_TRADE":
        entry_condition = "n/a"
        invalidation = "n/a"
        target_logic = "n/a"
        explanation = (
            f"combined_score={combined_score:+.3f} did not clear the "
            f"±{effective_threshold:.3f} action threshold (regime={regime}, "
            f"base ±{ACTION_THRESHOLD}), or components disagree too "
            f"much to act. No trade is the correct output here, not a failure."
        )
        if meta_note:
            explanation += f" {meta_note}."
    else:
        direction = "above" if action == "BUY" else "below"
        entry_condition = (
            "immediate at prevailing price, gated on freshness/spread checks "
            "in the risk governor (percentile-based dynamic spread bands are "
            "a v1.1 upgrade once more streamed spread history accumulates)"
        )
        invalidation = (
            f"price closes {('below' if action == 'BUY' else 'above')} "
            f"entry - stop_distance ({stop_distance:.5f}), or regime flips to SHOCK"
            if stop_distance
            else "stop distance unavailable (ATR not computed) — do not trade"
        )
        target_logic = (
            f"{REWARD_RISK_MULTIPLE}:1 reward:risk off ATR-based stop "
            f"(stop={stop_distance:.5f}, target={take_profit_distance:.5f})"
            if stop_distance
            else "n/a"
        )
        weight_summary = ", ".join(f"{v.name}={effective_weights.get(v.name, 0.0):.0%}" for v in component_views)
        explanation = (
            f"combined_score={combined_score:+.3f} clears ±{effective_threshold:.3f} "
            f"(regime={regime}, base ±{ACTION_THRESHOLD}); weighted {weight_summary} "
            f"(regime-adjusted from base weights), each additionally scaled by its own confidence."
        )
        if meta_note:
            explanation += f" {meta_note}; confidence above is the meta-model's, not the heuristic's."

    return TradeDecision(
        instrument=instrument,
        time=now,
        action=action,
        confidence=combined_confidence,
        horizon=horizon,
        regime=regime,
        key_drivers=key_drivers,
        contrary_evidence=contrary_evidence,
        entry_condition=entry_condition,
        invalidation=invalidation,
        stop_distance=stop_distance,
        take_profit_distance=take_profit_distance,
        target_logic=target_logic,
        data_freshness=data_freshness,
        explanation=explanation,
        component_views=component_views,
    )

"""Currency-strength state (Autonomous Upgrade Spec sec. 10): "Maintain an
independent latent strength state for each major currency. The pair engine
should compare two currency states rather than trying to predict every pair
independently."

This is deliberately an aggregation layer, not new data collection — macro,
cross-market and news components already compute per-currency scores
internally (src/models/{macro,cross_market,news}_model.py) for pair-diffing;
this module is what was missing: exposing that per-currency state as a
first-class, directly-queryable object, plus a technical component that
didn't exist before (aggregated from the price model's own per-pair
predictions, already logged in the `predictions` table — no retraining
needed, just reading what run_loop.py already recorded).

Positioning (spec sec. 10's 6th component, CFTC-based, src/positioning/cftc.py)
is deliberately reported SEPARATELY from the weighted composite, not folded
in as a directional input like the other four. Crowding is genuinely
ambiguous in direction — a crowded long position can mean trend-
confirmation (momentum continuing) or reversal risk (overextended), and
real trading practice is divided on which. Folding it into the composite
score would silently bake in one unstated interpretation; exposing it
separately keeps that judgment call visible rather than hidden inside a
single number.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from src.models.cross_market_model import currency_cross_market_score
from src.models.macro_model import currency_macro_score
from src.models.news_model import currency_news_score
from src.positioning.cftc import crowding_score

CURRENCIES = ["USD", "EUR", "GBP", "JPY", "CAD", "AUD"]

# Technical gets the most weight — it's the only component derived from a
# model with an actual walk-forward backtest behind it (src/backtest/engine.py),
# same reasoning decision/fusion.py uses for weighting price highest there.
STRENGTH_WEIGHTS = {"technical": 0.30, "macro": 0.30, "cross_market": 0.25, "news": 0.15}


@dataclass(frozen=True)
class CurrencyStrengthState:
    currency: str
    technical_score: float
    technical_confidence: float
    macro_score: float
    macro_confidence: float
    cross_market_score: float
    cross_market_confidence: float
    news_score: float
    news_confidence: float
    composite_score: float  # -1 (weak) .. +1 (strong) — technical/macro/cross_market/news only
    composite_confidence: float
    positioning_crowding_score: float  # separate on purpose — see module docstring
    positioning_confidence: float
    computed_at: datetime


def technical_score_from_pair_p_ups(pair_p_ups: dict[str, float], currency: str) -> tuple[float, float]:
    """pair_p_ups: {"EUR_USD": 0.62, ...} — most recent price-model P(up)
    per pair. Aggregates sign-adjusted across every pair involving this
    currency: bullish on a XXX_USD pair is bearish for USD, not bullish."""
    contributions = []
    for pair, p_up in pair_p_ups.items():
        if "_" not in pair:
            continue
        base, quote = pair.split("_")
        if currency not in (base, quote):
            continue
        score = (p_up - 0.5) * 2  # -1..1, positive = bullish BASE
        if currency == quote:
            score = -score
        contributions.append(score)
    if not contributions:
        return 0.0, 0.0
    avg = sum(contributions) / len(contributions)
    # More corroborating pairs = more confidence, caps out at 2 pairs since
    # most currencies here only ever appear in 1-2 of the v1 pairs anyway.
    confidence = min(1.0, len(contributions) / 2)
    return avg, confidence


def compute_currency_strength(
    currency: str,
    pair_p_ups: dict[str, float],
    economic_events: list[dict],
    news_events: list[dict],
    market_indicator_rows: list[dict],
    now: datetime,
    positioning_history: list[dict] | None = None,
) -> CurrencyStrengthState:
    tech_score, tech_conf = technical_score_from_pair_p_ups(pair_p_ups, currency)
    macro_score, macro_conf = currency_macro_score(economic_events, currency, now)
    xmkt_score, xmkt_conf = currency_cross_market_score(market_indicator_rows, currency)
    news_score, news_conf = currency_news_score(news_events, currency, now)
    positioning_score, positioning_conf = (
        crowding_score(positioning_history) if positioning_history else (0.0, 0.0)
    )

    parts = [
        (STRENGTH_WEIGHTS["technical"], tech_score, tech_conf),
        (STRENGTH_WEIGHTS["macro"], macro_score, macro_conf),
        (STRENGTH_WEIGHTS["cross_market"], xmkt_score, xmkt_conf),
        (STRENGTH_WEIGHTS["news"], news_score, news_conf),
    ]
    numerator = sum(w * s * c for w, s, c in parts)
    denominator = sum(w * c for w, s, c in parts)
    composite_score = numerator / denominator if denominator > 0 else 0.0
    total_weight = sum(w for w, _, _ in parts)
    composite_confidence = min(1.0, abs(composite_score) * (denominator / total_weight)) if total_weight else 0.0

    return CurrencyStrengthState(
        currency=currency,
        technical_score=tech_score, technical_confidence=tech_conf,
        macro_score=macro_score, macro_confidence=macro_conf,
        cross_market_score=xmkt_score, cross_market_confidence=xmkt_conf,
        news_score=news_score, news_confidence=news_conf,
        composite_score=composite_score, composite_confidence=composite_confidence,
        positioning_crowding_score=positioning_score, positioning_confidence=positioning_conf,
        computed_at=now,
    )


def compute_all_currency_strengths(
    pair_p_ups: dict[str, float],
    economic_events: list[dict],
    news_events: list[dict],
    market_indicator_rows: list[dict],
    now: datetime,
    currencies: list[str] = CURRENCIES,
    positioning_by_currency: dict[str, list[dict]] | None = None,
) -> dict[str, CurrencyStrengthState]:
    positioning_by_currency = positioning_by_currency or {}
    return {
        c: compute_currency_strength(
            c, pair_p_ups, economic_events, news_events, market_indicator_rows, now,
            positioning_history=positioning_by_currency.get(c),
        )
        for c in currencies
    }

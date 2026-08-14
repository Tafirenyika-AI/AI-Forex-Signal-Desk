"""News-event component model (blueprint sec. 6, row 3).

sec. 4.3 rule enforced here: "Never let sentiment alone trade." This module
only ever produces one score in the ensemble — decision fusion (sec. 7
agreement gate) requires other components to agree before a headline can
influence a trade. Confidence is deliberately discounted by relevance and by
recency; low-relevance or stale headlines contribute almost nothing.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

RECENCY_HALF_LIFE_HOURS = 6.0
STALE_CUTOFF_HOURS = 48.0


def _recency_weight(publish_time: datetime, now: datetime) -> float:
    age_hours = max((now - publish_time).total_seconds() / 3600.0, 0.0)
    if age_hours > STALE_CUTOFF_HOURS:
        return 0.0
    return 0.5 ** (age_hours / RECENCY_HALF_LIFE_HOURS)


def currency_news_score(
    news_rows: list[dict[str, Any]], currency: str, now: datetime | None = None
) -> tuple[float, float]:
    """Returns (score, aggregate_confidence), both in [-1, 1] / [0, 1].
    score is a confidence- and recency-weighted average sentiment; a low
    aggregate_confidence means the decision layer should not lean on this
    component even if the score itself looks strong."""
    now = now or datetime.now(timezone.utc)
    relevant = [
        r
        for r in news_rows
        if currency in (r.get("currencies") or "").split(",")
        and r.get("sentiment_score") is not None
    ]
    if not relevant:
        return 0.0, 0.0

    weighted_sum = 0.0
    weight_total = 0.0
    for r in relevant:
        relevance = r.get("confidence") if r.get("confidence") is not None else 0.3
        recency = _recency_weight(r["publish_time"], now)
        w = relevance * recency
        weighted_sum += r["sentiment_score"] * w
        weight_total += w

    if weight_total == 0:
        return 0.0, 0.0

    score = max(-1.0, min(1.0, weighted_sum / weight_total))
    # aggregate confidence caps out based on how much total weighted evidence
    # there is, not just how many articles — one high-relevance fresh article
    # beats ten stale low-relevance ones.
    aggregate_confidence = min(1.0, weight_total / len(relevant))
    return score, aggregate_confidence


def pair_news_score(
    news_rows: list[dict[str, Any]], pair: str, now: datetime | None = None
) -> tuple[float, float]:
    base, quote = pair.split("_")
    base_score, base_conf = currency_news_score(news_rows, base, now)
    quote_score, quote_conf = currency_news_score(news_rows, quote, now)
    score = max(-1.0, min(1.0, (base_score - quote_score) / 2))
    confidence = (base_conf + quote_conf) / 2
    return score, confidence

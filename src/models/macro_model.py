"""Macro-surprise component model (blueprint sec. 6, row 2).

v1 is rule-based, as the blueprint explicitly allows ("Rule + ML model").
Since only FRED-derived actual-vs-previous data is available (no analyst
consensus — see src/macro/fred.py docstring), this scores actual-vs-prior
drift, not true consensus surprise. Treat its confidence accordingly; the
decision fusion layer should weight it lower than a component with real
consensus data until a paid calendar is wired in.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

# Whether a higher `actual` than `previous` is bullish (+1) or bearish (-1)
# for that event's currency. Same rule family repeats per country: higher
# inflation/rates/growth => hawkish/strong => bullish; higher unemployment
# => weak => bearish. Keep this in sync with src/macro/fred.py's KEY_SERIES
# event names — an entry here with no matching series is inert, and a
# series with no entry here contributes nothing (direction defaults to 0).
EVENT_DIRECTION = {
    "US CPI (headline, SA)": 1,
    "US Nonfarm Payrolls": 1,
    "US Unemployment Rate": -1,
    "US Fed Funds Rate": 1,
    "US GDP": 1,
    "EUR Deposit Facility Rate": 1,
    "Euro Area HICP": 1,
    "Euro Area Unemployment Rate": -1,
    "Euro Area GDP": 1,
    "UK Bank Rate": 1,
    "UK CPI": 1,
    "UK Unemployment Rate": -1,
    "UK GDP": 1,
    "Japan Policy Rate": 1,
    "Japan CPI": 1,
    "Japan Unemployment Rate": -1,
    "Japan GDP": 1,
    "Canada Policy Rate": 1,
    "Canada CPI": 1,
    "Canada Unemployment Rate": -1,
    "Canada GDP": 1,
    "Australia CPI": 1,
    "Australia Unemployment Rate": -1,
    "Australia GDP": 1,
}

RECENCY_HALF_LIFE_DAYS = 30.0
MAX_EFFECTIVE_WEIGHT = 3.0  # weight_total needed to hit full confidence=1.0


def _recency_weight(event_time: datetime, now: datetime) -> float:
    age_days = max((now - event_time).total_seconds() / 86400.0, 0.0)
    return 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)


def currency_macro_score(
    events: list[dict[str, Any]], currency: str, now: datetime | None = None
) -> tuple[float, float]:
    """Combines recent economic_events rows for one currency into
    (score, confidence), both in [-1, 1] / [0, 1]. Positive score = bullish.
    confidence is 0 with no data at all, and grows with how much recent,
    directionally-mapped evidence is available — mirrors news_model's
    interface so decision fusion can treat both components symmetrically."""
    now = now or datetime.now(timezone.utc)
    relevant = [e for e in events if e["currency"] == currency and e.get("actual") is not None and e.get("previous")]
    if not relevant:
        return 0.0, 0.0

    weighted_sum = 0.0
    weight_total = 0.0
    for e in relevant:
        direction = EVENT_DIRECTION.get(e["event_name"], 0)
        if direction == 0 or e["previous"] == 0:
            continue
        pct_change = (e["actual"] - e["previous"]) / abs(e["previous"])
        signed = direction * pct_change
        w = _recency_weight(e["event_time"], now)
        weighted_sum += signed * w
        weight_total += w

    if weight_total == 0:
        return 0.0, 0.0
    raw = weighted_sum / weight_total
    score = math.tanh(raw * 10)  # squash into [-1, 1]; *10 keeps small % moves meaningful
    confidence = min(1.0, weight_total / MAX_EFFECTIVE_WEIGHT)
    return score, confidence


def pair_macro_score(
    events: list[dict[str, Any]], pair: str, now: datetime | None = None
) -> tuple[float, float]:
    """pair like 'EUR_USD' -> (base_score - quote_score) / 2, averaged confidence.
    Neutral (no opinion) for non-forex instruments (Alpaca equities/crypto)
    — a currency-differential score doesn't apply to a single-name ticker."""
    if "_" not in pair:
        return 0.0, 0.0
    base, quote = pair.split("_")
    base_score, base_conf = currency_macro_score(events, base, now)
    quote_score, quote_conf = currency_macro_score(events, quote, now)
    score = max(-1.0, min(1.0, (base_score - quote_score) / 2))
    confidence = (base_conf + quote_conf) / 2
    return score, confidence

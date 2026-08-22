"""Cross-market confirmation component (blueprint sec. 6, row 5).

v1 covers what's freely available and well-established (Appendix A):
- USD side: US Treasury yield trend + broad-dollar-index trend, both read
  as "rising = broad USD strength."
- CAD side: WTI crude trend (Appendix A explicitly calls out CAD's oil
  sensitivity).
No cross-market signal is defined yet for EUR/GBP/JPY/AUD specifically —
they fall back to a neutral 0.0/0.0 (no opinion) rather than guessing.
AUD is the natural next candidate (commodity-linked, e.g. iron ore/copper)
but that data source isn't wired up yet — better to say nothing than fake it.

Trends are self-relative (z-score against the indicator's own recent
volatility, squashed through tanh) rather than a fixed magic-number
threshold, matching the same philosophy as the regime detector.
"""
from __future__ import annotations

import math
from typing import Any

SHORT_WINDOW = 5
LONG_WINDOW = 20
MIN_OBSERVATIONS = 10


def _series_values(indicator_rows: list[dict[str, Any]], indicator: str) -> list[float]:
    rows = [r for r in indicator_rows if r["indicator"] == indicator]
    rows.sort(key=lambda r: r["observation_date"])
    return [r["value"] for r in rows]


def _trend_score(values: list[float]) -> tuple[float, float]:
    """(score, confidence). score in [-1, 1]: positive = recently trending
    up relative to its own volatility. confidence reflects how much history
    is actually available, not the strength of the move."""
    if len(values) < MIN_OBSERVATIONS:
        return 0.0, 0.0

    long_slice = values[-LONG_WINDOW:]
    short_slice = values[-SHORT_WINDOW:]
    long_mean = sum(long_slice) / len(long_slice)
    short_mean = sum(short_slice) / len(short_slice)

    variance = sum((v - long_mean) ** 2 for v in long_slice) / len(long_slice)
    std_dev = math.sqrt(variance)
    if std_dev == 0:
        return 0.0, min(1.0, len(values) / LONG_WINDOW)

    z = (short_mean - long_mean) / std_dev
    score = math.tanh(z)
    confidence = min(1.0, len(values) / LONG_WINDOW)
    return score, confidence


def usd_cross_market_score(indicator_rows: list[dict[str, Any]]) -> tuple[float, float]:
    yield_trend, yield_conf = _trend_score(_series_values(indicator_rows, "US10Y"))
    dollar_trend, dollar_conf = _trend_score(_series_values(indicator_rows, "USD_INDEX"))

    weights = [(yield_trend, yield_conf), (dollar_trend, dollar_conf)]
    total_conf = sum(c for _, c in weights)
    if total_conf == 0:
        return 0.0, 0.0
    score = sum(s * c for s, c in weights) / total_conf
    confidence = total_conf / len(weights)
    return score, confidence


def cad_oil_score(indicator_rows: list[dict[str, Any]]) -> tuple[float, float]:
    return _trend_score(_series_values(indicator_rows, "WTI_OIL"))


def currency_cross_market_score(indicator_rows: list[dict[str, Any]], currency: str) -> tuple[float, float]:
    if currency == "USD":
        return usd_cross_market_score(indicator_rows)
    if currency == "CAD":
        return cad_oil_score(indicator_rows)
    return 0.0, 0.0  # no signal defined yet — honest neutral, not a guess


def pair_cross_market_score(indicator_rows: list[dict[str, Any]], pair: str) -> tuple[float, float]:
    """Neutral (no opinion) for non-forex instruments (Alpaca equities/
    crypto) — a currency-differential score doesn't apply to a single-name
    ticker."""
    if "_" not in pair:
        return 0.0, 0.0
    base, quote = pair.split("_")
    base_score, base_conf = currency_cross_market_score(indicator_rows, base)
    quote_score, quote_conf = currency_cross_market_score(indicator_rows, quote)
    score = max(-1.0, min(1.0, (base_score - quote_score) / 2))
    confidence = (base_conf + quote_conf) / 2
    return score, confidence

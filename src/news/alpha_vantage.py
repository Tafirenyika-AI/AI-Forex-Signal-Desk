"""News & Current-Affairs Gateway - Alpha Vantage adapter (blueprint sec. 4.3).

Get a free key (instant, no cost) at:
https://www.alphavantage.co/support/#api-key

Free tier is rate-limited (a handful of requests/minute, ~25/day on the
lowest tier at time of writing) — fine for periodic polling, not for a
tight event-reaction loop. Upgrade path noted in blueprint sec. 4.3.

Observed in practice (2026-08-12): querying by `tickers=FOREX:...` returns
articles that are, in large part, recycled equity-analysis pieces with only
a weak/incidental forex tag (relevance ~0.05) and publish dates spread over
months, not a genuine forex-focused "latest news" stream. This is a real
free-tier coverage gap, not a bug in this adapter — src/models/news_model.py's
recency half-life (6h) and staleness cutoff (48h) correctly zero these out
rather than let stale, weakly-tagged sentiment influence a decision. Expect
this component to contribute close to nothing until either Alpha Vantage's
forex coverage improves or it's replaced with a licensed feed (sec. 4.3).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

ALPHA_VANTAGE_BASE_URL = "https://www.alphavantage.co/query"

CURRENCY_TICKERS = {
    "USD": "FOREX:USD",
    "EUR": "FOREX:EUR",
    "GBP": "FOREX:GBP",
    "JPY": "FOREX:JPY",
    "CAD": "FOREX:CAD",
    "AUD": "FOREX:AUD",
}

# Empirically: Alpha Vantage's `topics` filter alone almost never surfaces
# FOREX: ticker_sentiment entries (it mostly returns equity-tagged articles
# even for macro topics), and combining `tickers` with `topics` uses AND
# semantics that returns zero results against the free-tier corpus. Querying
# by `tickers` alone is what actually returns FOREX:-tagged sentiment.
# Relevance scores on those tags run low (often 0.05-0.1) — that's real
# free-tier coverage, not a bug here; news_model.py's confidence weighting
# already discounts low-relevance items so they can't swing a decision much.
FOREX_TICKERS = ",".join(CURRENCY_TICKERS.values())


class AlphaVantageNewsClient:
    def __init__(self, api_key: str | None):
        self.api_key = api_key
        self._client = httpx.AsyncClient(base_url=ALPHA_VANTAGE_BASE_URL, timeout=15.0)

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "AlphaVantageNewsClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def fetch_forex_news(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self.configured:
            return []

        params = {
            "function": "NEWS_SENTIMENT",
            "tickers": FOREX_TICKERS,
            "apikey": self.api_key,
            "limit": str(limit),
            "sort": "LATEST",
        }
        response = await self._client.get("", params=params)
        response.raise_for_status()
        data = response.json()
        if "feed" not in data:
            # Alpha Vantage returns {"Information": "..."} on rate limit / bad key
            # instead of an HTTP error. Surface it rather than silently returning [].
            raise RuntimeError(f"Alpha Vantage did not return a feed: {data}")
        return self._normalize(data["feed"])

    def _normalize(self, feed: list[dict[str, Any]]) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        rows = []
        for item in feed:
            currencies = self._extract_currencies(item)
            if not currencies:
                continue
            publish_time = self._parse_av_time(item["time_published"])
            rows.append(
                {
                    "publish_time": publish_time,
                    "ingest_time": now,
                    "source": item.get("source", "alphavantage"),
                    "headline": item.get("title", ""),
                    "url": item.get("url"),
                    "currencies": ",".join(sorted(currencies)),
                    "event_type": ",".join(t["topic"] for t in item.get("topics", [])),
                    "sentiment_score": self._safe_float(item.get("overall_sentiment_score")),
                    "novelty_score": None,  # requires a dedup/similarity pass; not in v1
                    "confidence": self._relevance_confidence(item, currencies),
                }
            )
        return rows

    def _extract_currencies(self, item: dict[str, Any]) -> set[str]:
        found = set()
        for ticker_sentiment in item.get("ticker_sentiment", []):
            ticker = ticker_sentiment.get("ticker", "")
            if ticker.startswith("FOREX:"):
                code = ticker.split(":", 1)[1]
                if code in CURRENCY_TICKERS:
                    found.add(code)
        return found

    def _relevance_confidence(self, item: dict[str, Any], currencies: set[str]) -> float | None:
        relevances = [
            self._safe_float(t.get("relevance_score"))
            for t in item.get("ticker_sentiment", [])
            if t.get("ticker", "").split(":")[-1] in currencies
        ]
        relevances = [r for r in relevances if r is not None]
        return max(relevances) if relevances else None

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_av_time(value: str) -> datetime:
        # Alpha Vantage format: YYYYMMDDTHHMMSS
        return datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)

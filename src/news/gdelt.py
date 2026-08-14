"""GDELT news integration (Autonomous Upgrade Spec sec. 2 "News & Events
Brain", sec. 8 "News Intelligence Beyond Sentiment").

Verified live 2026-08-13 against https://api.gdeltproject.org/api/v2/doc/doc
— free, no API key, real current articles (same-day inflation/Treasury
coverage in the first test query). The one real constraint: GDELT's own
429 response is explicit — "limit requests to one every 5 seconds" — a
per-caller pacing limit, not a shared global bucket like the Forex Factory
calendar feed. This client paces itself with a safety margin rather than
relying on the caller to space calls out correctly.

Per spec sec. 8's own framing, this deliberately does NOT lean on language
sentiment as the primary signal — "measure price reaction... rather than
relying only on language sentiment." What GDELT actually gives us over
Alpha Vantage (src/news/alpha_vantage.py, confirmed to contribute zero
signal in practice) is coverage breadth and freshness: real currency-
targeted queries against a global news index, with clustering across
domains standing in for the "how corroborated is this" signal the spec
asks for (sec. 8 "Cluster duplicate headlines", sec. 4.2 "Independence").
Sentiment here is a lightweight finance-keyword heuristic — same honesty
caveat as everywhere else rule-based in this project: it's a coarse proxy,
not language understanding.
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx

GDELT_BASE_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
# GDELT's 429 body states "one every 5 seconds," but repeated live testing
# 2026-08-13 kept hitting 429s even well past that spacing — the enforced
# window appears stricter in practice than the documented figure (possibly
# a per-minute budget, not a strict per-request cadence). Paced
# conservatively rather than trusting the documented number literally.
MIN_REQUEST_INTERVAL_SECONDS = 15.0

# Currency-targeted queries, matching this system's existing 5 v1 pairs (sec.
# 6's full 8-currency list includes CHF/NZD, which aren't traded here yet —
# extend this dict if those pairs are ever added).
CURRENCY_QUERIES = {
    "USD": '("Federal Reserve" OR FOMC OR "US inflation" OR "US Treasury yields")',
    "EUR": '("European Central Bank" OR ECB OR Eurozone OR "euro area")',
    "GBP": '("Bank of England" OR sterling OR "UK inflation" OR "British pound")',
    "JPY": '("Bank of Japan" OR BOJ OR yen OR "Japanese yen")',
    "CAD": '("Bank of Canada" OR "Canadian dollar" OR loonie)',
    "AUD": '("Reserve Bank of Australia" OR RBA OR "Australian dollar")',
}

POSITIVE_KEYWORDS = [
    "rises", "rose", "gains", "gained", "rally", "rallies", "surge", "surges",
    "strengthens", "strengthened", "climbs", "climbed", "beats expectations",
    "better than expected", "upbeat", "optimis", "hawkish", "tightening",
    "outperform", "recovery", "rebound",
]
NEGATIVE_KEYWORDS = [
    "falls", "fell", "drops", "dropped", "slumps", "slump", "plunge", "plunges",
    "weakens", "weakened", "declines", "declined", "misses expectations",
    "worse than expected", "recession", "crisis", "dovish", "easing",
    "cuts rates", "sell-off", "selloff", "downturn", "contraction",
]


def title_sentiment(title: str) -> float:
    lowered = title.lower()
    pos = sum(1 for kw in POSITIVE_KEYWORDS if kw in lowered)
    neg = sum(1 for kw in NEGATIVE_KEYWORDS if kw in lowered)
    if pos == 0 and neg == 0:
        return 0.0
    return max(-1.0, min(1.0, (pos - neg) / (pos + neg)))


def _normalize_title(title: str) -> str:
    stripped = re.sub(r"[^a-z0-9 ]", "", title.lower())
    return re.sub(r"\s+", " ", stripped).strip()


class GDELTClient:
    def __init__(self):
        self._client = httpx.AsyncClient(timeout=20.0)
        self._last_request_monotonic: float | None = None

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "GDELTClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def _paced_get(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._last_request_monotonic is not None:
            elapsed = time.monotonic() - self._last_request_monotonic
            if elapsed < MIN_REQUEST_INTERVAL_SECONDS:
                await asyncio.sleep(MIN_REQUEST_INTERVAL_SECONDS - elapsed)
        self._last_request_monotonic = time.monotonic()

        try:
            response = await self._client.get(GDELT_BASE_URL, params=params)
        except httpx.TransportError:
            # GDELT's server occasionally drops the connection mid-request
            # (verified live 2026-08-13) — one failed currency query should
            # not crash the whole multi-currency ingestion run.
            return {}
        if response.status_code != 200:
            return {}
        try:
            return response.json()
        except ValueError:
            return {}

    async def fetch_currency_articles(
        self, currency: str, timespan: str = "6h", maxrecords: int = 30
    ) -> list[dict[str, Any]]:
        query = CURRENCY_QUERIES.get(currency)
        if not query:
            return []
        data = await self._paced_get(
            {
                "query": query, "mode": "artlist", "format": "json",
                "maxrecords": maxrecords, "sort": "datedesc", "timespan": timespan,
            }
        )
        articles = data.get("articles", [])
        for a in articles:
            a["_matched_currency"] = currency
            a["_query_keyword_in_title"] = any(
                kw.strip('"()').lower() in a.get("title", "").lower()
                for kw in re.findall(r'"[^"]+"|\b[A-Za-z]+\b', query)
                if len(kw.strip('"()')) > 2
            )
        return articles

    async def fetch_all_currencies(
        self, currencies: list[str] | None = None, timespan: str = "6h"
    ) -> list[dict[str, Any]]:
        """Fetches each currency's query in sequence (paced) and merges by
        URL — the same article often matches multiple currency queries
        (e.g. "EUR/USD rises as Fed signals pause"), and news_events has a
        UNIQUE constraint on url, so this must produce one row per URL with
        the union of matched currencies, not one row per (currency, url)."""
        currencies = currencies or list(CURRENCY_QUERIES.keys())
        by_url: dict[str, dict[str, Any]] = {}

        for currency in currencies:
            articles = await self.fetch_currency_articles(currency, timespan=timespan)
            for a in articles:
                url = a.get("url")
                if not url:
                    continue
                if url not in by_url:
                    by_url[url] = {**a, "_currencies": {currency}}
                else:
                    by_url[url]["_currencies"].add(currency)

        # Clustering (sec. 8 "cluster duplicate headlines"): group by
        # normalized title across the whole batch so wire-service reprints
        # count as one story corroborated by N domains, not N separate votes.
        by_normalized_title: dict[str, list[dict[str, Any]]] = {}
        for row in by_url.values():
            key = _normalize_title(row.get("title", ""))
            by_normalized_title.setdefault(key, []).append(row)

        results = []
        for _, group in by_normalized_title.items():
            primary = group[0]
            distinct_domains = {g.get("domain") for g in group if g.get("domain")}
            primary["_distinct_domain_count"] = len(distinct_domains)
            primary["_currencies"] = set().union(*(g["_currencies"] for g in group))
            results.append(primary)
        return results

"""Equity V2 Phase 4 — SEC EDGAR structured financial intelligence.

Official source only (data.sec.gov), not an LLM summary of a filing: SEC's
own XBRL "company concept" API returns exactly what a company filed, as
filed, with the real publish timestamp (`filed`) kept distinct from the
fiscal period it describes (`end`/`start`) — the same point-in-time split
src/data/db.py's company_fundamentals table was built around (Phase 3).

Verified live before writing this (2026-10-01/02, real NVDA, CIK 1045810):

    GET https://www.sec.gov/files/company_tickers.json
        -> {"0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"}, ...}

    GET https://data.sec.gov/api/xbrl/companyconcept/CIK0001045810/us-gaap/Revenues.json
        -> {"cik": ..., "taxonomy": "us-gaap", "tag": "Revenues", "label": ...,
            "units": {"USD": [
                {"start": "2026-01-26", "end": "2026-04-26", "val": 81615000000,
                 "accn": "0001045810-26-000052", "fy": 2027, "fp": "Q1",
                 "form": "10-Q", "filed": "2026-05-20", "frame": "CY2026Q1"},
                ...
            ]}}

A single us-gaap tag can report BOTH a single-quarter fact and a
cumulative year-to-date fact that share the SAME `end` date (confirmed
live: NVDA's Q2 FY2027 Revenues appear both as a quarterly value,
start=2026-04-27, and a cumulative H1 value, start=2026-01-26, both
end=2026-07-26, same `filed`/`accn`) — distinguished only by `start`.
company_fundamentals.period_start exists, and is part of its unique
constraint, for exactly this reason (see that column's own comment in
src/data/db.py).

SEC's fair-access policy requires a descriptive User-Agent identifying the
real requester, not a generic one, or requests get throttled/blocked:
https://www.sec.gov/os/webmaster-faq#developers — confirmed by this
project's own live verification call before writing the parser.

This module only ever performs GET requests against SEC's own public,
unauthenticated, read-only endpoints. It has no broker/execution
dependency whatsoever — not even an import of src.execution or
src.broker — so it has zero capacity to affect any order or position,
structurally, not just by convention.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger("equity.sec_edgar")

SEC_BASE_URL = "https://data.sec.gov"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

# A deliberately small, high-signal set of us-gaap tags aimed at Phase 5's
# growth/margin/cash-flow feature engine — not an attempt to ingest every
# XBRL concept a company might report. Not every company tags every one of
# these (older filers, and financials/REITs/insurers use different
# statement shapes entirely) — fetch_concept treats a 404 as "this company
# doesn't report this tag," never an error, and a missing tag is simply
# skipped rather than fabricated.
KEY_METRICS: list[str] = [
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",  # the common post-ASC606 revenue tag
    "NetIncomeLoss",
    "OperatingIncomeLoss",
    "GrossProfit",
    "NetCashProvidedByUsedInOperatingActivities",
    "Assets",
    "Liabilities",
    "StockholdersEquity",
    "EarningsPerShareDiluted",
]


def _parse_sec_date(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    return datetime.combine(date.fromisoformat(raw), datetime.min.time(), tzinfo=timezone.utc)


class SecEdgarClient:
    """One client per process is enough — the ticker->CIK map is fetched
    once and cached for the client's lifetime (it's a ~10K-row static file
    SEC republishes periodically, not something that needs re-fetching
    per-ticker-lookup)."""

    def __init__(self, contact_email: str):
        if not contact_email:
            raise ValueError("SEC EDGAR requires a real contact email in its User-Agent (fair-access policy).")
        self._headers = {"User-Agent": f"AI-Forex-Signal-Desk ({contact_email})"}
        self._client = httpx.AsyncClient(headers=self._headers, timeout=15.0)
        self._ticker_to_cik: dict[str, str] | None = None

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "SecEdgarClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def _load_ticker_map(self) -> dict[str, dict[str, Any]]:
        if self._ticker_to_cik is None:
            response = await self._client.get(SEC_TICKERS_URL)
            response.raise_for_status()
            self._ticker_to_cik = {
                row["ticker"].upper(): {"cik": str(row["cik_str"]).zfill(10), "title": row["title"]}
                for row in response.json().values()
            }
        return self._ticker_to_cik

    async def lookup(self, ticker: str) -> dict[str, Any] | None:
        """Returns {"cik": "0001045810", "title": "NVIDIA CORP"} or None if
        this ticker isn't a SEC-registered filer (e.g. most crypto pairs,
        or a ticker typo) — not every Alpaca-tradable symbol has a CIK."""
        mapping = await self._load_ticker_map()
        return mapping.get(ticker.upper())

    async def fetch_concept(self, cik: str, tag: str) -> dict[str, Any] | None:
        """A single us-gaap concept's full reported history for one
        company. None (not an exception) when this company has never
        tagged this concept — a real, common, non-error outcome."""
        url = f"{SEC_BASE_URL}/api/xbrl/companyconcept/CIK{cik}/us-gaap/{tag}.json"
        response = await self._client.get(url)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    async def fundamentals_rows(self, ticker: str, cik: str) -> list[dict[str, Any]]:
        """Normalized rows ready for an insert into company_fundamentals,
        across every tag in KEY_METRICS this company actually reports."""
        rows: list[dict[str, Any]] = []
        for tag in KEY_METRICS:
            concept = await self.fetch_concept(cik, tag)
            if concept is None:
                continue
            for unit, facts in concept.get("units", {}).items():
                for fact in facts:
                    row = _fact_to_row(ticker, tag, unit, fact)
                    if row is not None:
                        rows.append(row)
        return rows

    def entity_row(self, ticker: str, cik: str, company_name: str) -> dict[str, Any]:
        return {
            "ticker": ticker.upper(),
            "cik": cik,
            "company_name": company_name,
            "sector": None,  # not provided by this endpoint -- Phase 7's relationship graph fills this in
            "industry": None,
            "exchange": None,
            "sector_etf": None,
            "index_membership": None,
            "source": "sec_edgar",
            "updated_at": datetime.now(timezone.utc),
        }


def _fact_to_row(ticker: str, tag: str, unit: str, fact: dict[str, Any]) -> dict[str, Any] | None:
    """Pure transform, isolated from the network call so it's directly
    unit-testable against real captured fact shapes without hitting SEC."""
    end_raw = fact.get("end")
    filed_raw = fact.get("filed")
    if not end_raw or not filed_raw:
        return None  # a shape this client doesn't recognize -- skip, don't guess
    period_end = _parse_sec_date(end_raw)
    # Instant/balance-sheet metrics have no `start` in SEC's own response --
    # period_start = period_end is the honest "this is a single point, not
    # a duration" sentinel company_fundamentals.period_start requires (see
    # its own NOT NULL column comment: a nullable version let duplicate
    # instant-metric rows bypass the unique constraint entirely).
    period_start = _parse_sec_date(fact.get("start")) or period_end
    return {
        "ticker": ticker.upper(),
        "period_start": period_start,
        "period_end": period_end,
        "filed_at": _parse_sec_date(filed_raw),
        "fiscal_period": f"{fact.get('fp', '?')}-FY{fact.get('fy', '?')}",
        "form_type": fact.get("form", "unknown"),
        "metric": tag,
        "value": fact.get("val"),
        "unit": unit,
        "source": "sec_edgar",
        "accession_number": fact.get("accn"),
    }

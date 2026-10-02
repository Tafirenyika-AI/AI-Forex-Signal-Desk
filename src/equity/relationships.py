"""Equity V2 Phase 7 — equity relationship/knowledge graph.

Builds ticker -> company -> SIC industry -> sector -> sector ETF -> real
exchange from SEC's own submissions endpoint
(data.sec.gov/submissions/CIK##########.json) — the same official-
source-only standard Phase 4 established, reusing the CIK Phase 4 already
wrote into equity_entities. Verified live (2026-10-02, real NVDA CIK
1045810): the real response includes `sic` ("3674"), `sicDescription`
("Semiconductors & Related Devices"), and `exchanges` (["Nasdaq"]).

Honesty note, load-bearing for how this should be used: SEC's SIC
(Standard Industrial Classification) code is a real, decades-old US
government classification — it is NOT the same thing as GICS, the sector
taxonomy Wall Street actually uses for "the 11 S&P sectors." SIC_TO_SECTOR
below is a disclosed, maintainable APPROXIMATION mapping SIC code ranges
to the closest of the 11 real SPDR Select Sector ETFs (XLK/XLF/XLV/...),
not an authoritative GICS classification — good enough for Phase 8's
sector-relative cross-market features, not a substitute for a licensed
GICS feed this project doesn't have.

Peers (the brief's own explicit ask: "a maintainable mapping, not a
hardcoded handful of famous stocks") are derived dynamically from this
project's own growing equity_entities table — other tickers sharing the
same real SIC code — rather than a static hand-picked list that would
silently go stale and never include a newly-added ticker.

Index membership (e.g. "SPX"/"NDX") is deliberately left unpopulated by
this phase: no free, reliable, point-in-time-correct index-constituent
source was found that doesn't require a paid license or scraping a page
with no stability/accuracy guarantee. Leaving it None and disclosed is
more honest than hardcoding today's constituents as if they were
authoritative and permanently correct.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.data.db import equity_entities as equity_entities_table

SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"

# SIC code range (inclusive) -> (sector label, representative SPDR Select
# Sector ETF). Checked in order; the FIRST matching range wins, so more
# specific/narrower ranges are listed before broader catch-alls (e.g.
# semiconductors before the general electronics range). Coverage is
# intentionally broad rather than exhaustive — an SIC code outside every
# range below (e.g. public administration, 9100-9999) is left
# unclassified (None), never force-fit into a sector it doesn't belong in.
SIC_TO_SECTOR: list[tuple[range, tuple[str, str]]] = [
    # Narrow ranges that nest INSIDE a broader range listed further down
    # must come first -- real bug caught by this module's own test:
    # pharmaceuticals (2834) nests inside the general chemicals range
    # (2800-2900) and was being swallowed by it when chemicals was checked
    # first, miscategorizing every real pharma ticker as Materials instead
    # of Health Care.
    (range(2830, 2840), ("Health Care", "XLV")),  # pharmaceuticals -- must precede the general chemicals range below
    (range(3826, 3828), ("Health Care", "XLV")),  # lab/medical instruments
    (range(3570, 3580), ("Technology", "XLK")),  # computer hardware -- must precede the general electronic-equipment range below
    (range(1300, 1400), ("Energy", "XLE")),
    (range(2900, 3000), ("Energy", "XLE")),
    (range(1000, 1300), ("Materials", "XLB")),
    (range(1400, 1500), ("Materials", "XLB")),
    (range(2600, 2700), ("Materials", "XLB")),
    (range(2800, 2900), ("Materials", "XLB")),  # general chemicals -- pharma carve-out above takes precedence
    (range(3000, 3100), ("Materials", "XLB")),
    (range(3200, 3400), ("Materials", "XLB")),
    (range(1500, 1800), ("Industrials", "XLI")),
    (range(3400, 3600), ("Industrials", "XLI")),
    (range(3700, 3800), ("Industrials", "XLI")),
    (range(3900, 4000), ("Industrials", "XLI")),
    (range(4000, 4800), ("Industrials", "XLI")),
    (range(3600, 3700), ("Technology", "XLK")),  # electronic equipment, incl. semiconductors (3674)
    (range(7370, 7380), ("Technology", "XLK")),  # prepackaged software / computer services
    (range(4800, 4900), ("Communication Services", "XLC")),
    (range(2700, 2800), ("Communication Services", "XLC")),  # publishing
    (range(7800, 7840), ("Communication Services", "XLC")),  # motion pictures
    (range(4900, 5000), ("Utilities", "XLU")),
    (range(6000, 6400), ("Financials", "XLF")),
    (range(6700, 6800), ("Financials", "XLF")),
    (range(6500, 6600), ("Real Estate", "XLRE")),
    (range(8000, 8100), ("Health Care", "XLV")),
    (range(2000, 2200), ("Consumer Staples", "XLP")),
    (range(5410, 5420), ("Consumer Staples", "XLP")),  # grocery stores
    (range(2200, 2400), ("Consumer Discretionary", "XLY")),
    (range(5200, 5410), ("Consumer Discretionary", "XLY")),
    (range(5420, 6000), ("Consumer Discretionary", "XLY")),
    (range(5000, 5200), ("Consumer Discretionary", "XLY")),
    (range(7000, 7370), ("Consumer Discretionary", "XLY")),
    (range(7380, 7800), ("Consumer Discretionary", "XLY")),
]


def sic_to_sector(sic_code: str | None) -> tuple[str | None, str | None]:
    """(sector, sector_etf), both None if the code is missing or falls
    outside every mapped range -- never a guessed default sector."""
    if not sic_code:
        return None, None
    try:
        code = int(sic_code)
    except ValueError:
        return None, None
    for sic_range, (sector, etf) in SIC_TO_SECTOR:
        if code in sic_range:
            return sector, etf
    return None, None


class SecRelationshipClient:
    def __init__(self, contact_email: str):
        if not contact_email:
            raise ValueError("SEC EDGAR requires a real contact email in its User-Agent (fair-access policy).")
        self._client = httpx.AsyncClient(headers={"User-Agent": f"AI-Forex-Signal-Desk ({contact_email})"}, timeout=15.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "SecRelationshipClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def fetch_submission(self, cik: str) -> dict[str, Any]:
        response = await self._client.get(SEC_SUBMISSIONS_URL.format(cik=cik))
        response.raise_for_status()
        return response.json()

    async def enrichment_row(self, ticker: str, cik: str) -> dict[str, Any]:
        submission = await self.fetch_submission(cik)
        sic_code = submission.get("sic")
        sector, sector_etf = sic_to_sector(sic_code)
        exchanges = submission.get("exchanges") or []
        return {
            "ticker": ticker.upper(),
            "sic_code": sic_code,
            "industry": submission.get("sicDescription"),
            "sector": sector,
            "sector_etf": sector_etf,
            "exchange": exchanges[0] if exchanges else None,
            "updated_at": datetime.now(timezone.utc),
        }


def peers_for(engine: Engine, ticker: str) -> list[str]:
    """Other tickers in this project's own equity_entities table sharing
    the same real SIC code — dynamic, grows as more tickers are synced,
    never a static hand-picked list. Empty (not an error) if this ticker
    has no sic_code yet, or no other ticker shares it."""
    with engine.connect() as conn:
        this_sic = conn.execute(
            select(equity_entities_table.c.sic_code).where(equity_entities_table.c.ticker == ticker.upper())
        ).scalar()
        if not this_sic:
            return []
        rows = conn.execute(
            select(equity_entities_table.c.ticker).where(
                equity_entities_table.c.sic_code == this_sic,
                equity_entities_table.c.ticker != ticker.upper(),
            )
        ).fetchall()
    return sorted(r.ticker for r in rows)

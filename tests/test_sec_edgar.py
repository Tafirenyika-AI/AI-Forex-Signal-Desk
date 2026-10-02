"""Unit tests for Equity V2 Phase 4: src/equity/sec_edgar.py.

Uses httpx.MockTransport (a real httpx feature, not a hand-rolled fake) to
replay scripted HTTP responses rather than hitting the real network — the
real protocol itself (real response shapes for both the ticker->CIK
mapping and a companyconcept lookup, including the real quarterly-vs-YTD
same-end-date quirk) was separately verified live against the actual SEC
EDGAR API before this module was written (see its own docstring).

Run: .venv/Scripts/python.exe -m pytest tests/test_sec_edgar.py -v
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

import httpx
import pytest

from src.equity.sec_edgar import KEY_METRICS, SecEdgarClient, _fact_to_row, _parse_sec_date


def _run(coro):
    return asyncio.run(coro)


def _swap_transport(client: SecEdgarClient, handler) -> None:
    """Replaces the client's real httpx.AsyncClient with one wired to a
    MockTransport -- same internal-state-poking style already used for
    AlpacaMarketStream's direct state manipulation tests."""
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=client._headers)


# --- pure parsing: _fact_to_row / _parse_sec_date ---

def test_parse_sec_date():
    assert _parse_sec_date("2026-07-26") == datetime(2026, 7, 26, tzinfo=timezone.utc)
    assert _parse_sec_date(None) is None


def test_fact_to_row_quarterly_fact():
    # Real captured shape, NVDA Q2 FY2027 Revenues (verified live 2026-10-01).
    fact = {"start": "2026-04-27", "end": "2026-07-26", "val": 96221000000,
            "accn": "0001045810-26-000075", "fy": 2027, "fp": "Q2", "form": "10-Q",
            "filed": "2026-08-26", "frame": "CY2026Q2"}
    row = _fact_to_row("nvda", "Revenues", "USD", fact)
    assert row["ticker"] == "NVDA"
    assert row["period_start"] == datetime(2026, 4, 27, tzinfo=timezone.utc)
    assert row["period_end"] == datetime(2026, 7, 26, tzinfo=timezone.utc)
    assert row["filed_at"] == datetime(2026, 8, 26, tzinfo=timezone.utc)
    assert row["fiscal_period"] == "Q2-FY2027"
    assert row["form_type"] == "10-Q"
    assert row["metric"] == "Revenues"
    assert row["value"] == 96221000000
    assert row["unit"] == "USD"
    assert row["source"] == "sec_edgar"
    assert row["accession_number"] == "0001045810-26-000075"


def test_fact_to_row_ytd_fact_shares_end_date_with_quarterly_but_differs_by_start():
    # Same company/tag/end as the quarterly fact above, real captured
    # cumulative H1 value instead -- the exact live-verified quirk
    # company_fundamentals.period_start exists to disambiguate.
    fact = {"start": "2026-01-26", "end": "2026-07-26", "val": 177837000000,
            "accn": "0001045810-26-000075", "fy": 2027, "fp": "Q2", "form": "10-Q",
            "filed": "2026-08-26"}
    row = _fact_to_row("NVDA", "Revenues", "USD", fact)
    assert row["period_start"] == datetime(2026, 1, 26, tzinfo=timezone.utc)
    assert row["period_end"] == datetime(2026, 7, 26, tzinfo=timezone.utc)
    assert row["value"] == 177837000000


def test_fact_to_row_instant_metric_has_no_start_sets_period_start_equals_period_end():
    # Balance-sheet metrics (Assets, StockholdersEquity, ...) never carry a
    # `start` in SEC's own response -- the honest sentinel per
    # company_fundamentals.period_start's own NOT NULL design.
    fact = {"end": "2026-07-26", "val": 100000000000, "accn": "0001045810-26-000075",
            "fy": 2027, "fp": "Q2", "form": "10-Q", "filed": "2026-08-26"}
    row = _fact_to_row("NVDA", "Assets", "USD", fact)
    assert row["period_start"] == row["period_end"] == datetime(2026, 7, 26, tzinfo=timezone.utc)


def test_fact_to_row_missing_end_or_filed_returns_none():
    assert _fact_to_row("NVDA", "Revenues", "USD", {"val": 1, "filed": "2026-08-26"}) is None  # no end
    assert _fact_to_row("NVDA", "Revenues", "USD", {"val": 1, "end": "2026-07-26"}) is None  # no filed


# --- SecEdgarClient, driven by httpx.MockTransport ---

def test_client_requires_a_real_contact_email():
    with pytest.raises(ValueError):
        SecEdgarClient(contact_email="")


def test_lookup_known_ticker():
    tickers_json = json.dumps({"0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"}})

    def handler(request: httpx.Request) -> httpx.Response:
        assert "AI-Forex-Signal-Desk" in request.headers["User-Agent"]
        return httpx.Response(200, content=tickers_json)

    async def scenario():
        client = SecEdgarClient(contact_email="test@example.com")
        _swap_transport(client, handler)
        result = await client.lookup("nvda")
        await client.close()
        return result

    result = _run(scenario())
    assert result == {"cik": "0001045810", "title": "NVIDIA CORP"}


def test_lookup_unknown_ticker_returns_none():
    tickers_json = json.dumps({"0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"}})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=tickers_json)

    async def scenario():
        client = SecEdgarClient(contact_email="test@example.com")
        _swap_transport(client, handler)
        result = await client.lookup("NOT_A_REAL_TICKER")
        await client.close()
        return result

    assert _run(scenario()) is None


def test_fetch_concept_404_returns_none_not_raise():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    async def scenario():
        client = SecEdgarClient(contact_email="test@example.com")
        _swap_transport(client, handler)
        result = await client.fetch_concept("0001045810", "SomeTagThisCompanyNeverReported")
        await client.close()
        return result

    assert _run(scenario()) is None


def test_fetch_concept_success_returns_the_real_response_shape():
    concept_json = json.dumps({
        "cik": 1045810, "taxonomy": "us-gaap", "tag": "Revenues",
        "units": {"USD": [{"start": "2026-04-27", "end": "2026-07-26", "val": 96221000000,
                            "accn": "x", "fy": 2027, "fp": "Q2", "form": "10-Q", "filed": "2026-08-26"}]},
    })

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=concept_json)

    async def scenario():
        client = SecEdgarClient(contact_email="test@example.com")
        _swap_transport(client, handler)
        result = await client.fetch_concept("0001045810", "Revenues")
        await client.close()
        return result

    result = _run(scenario())
    assert result["tag"] == "Revenues"
    assert result["units"]["USD"][0]["val"] == 96221000000


def test_fundamentals_rows_skips_tags_the_company_never_reported():
    # Only the FIRST tag in KEY_METRICS is reported by this fake company;
    # every other tag 404s. fundamentals_rows must still return the one
    # real row, not raise, and must not fabricate rows for the 404s.
    reported_tag = KEY_METRICS[0]
    concept_json = json.dumps({
        "units": {"USD": [{"end": "2026-07-26", "val": 42, "accn": "x",
                            "fy": 2027, "fp": "Q2", "form": "10-Q", "filed": "2026-08-26"}]},
    })

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/{reported_tag}.json"):
            return httpx.Response(200, content=concept_json)
        return httpx.Response(404)

    async def scenario():
        client = SecEdgarClient(contact_email="test@example.com")
        _swap_transport(client, handler)
        rows = await client.fundamentals_rows("NVDA", "0001045810")
        await client.close()
        return rows

    rows = _run(scenario())
    assert len(rows) == 1
    assert rows[0]["metric"] == reported_tag
    assert rows[0]["value"] == 42


def test_entity_row_shape():
    client = SecEdgarClient(contact_email="test@example.com")
    row = client.entity_row("nvda", "0001045810", "NVIDIA CORP")
    assert row["ticker"] == "NVDA"
    assert row["cik"] == "0001045810"
    assert row["company_name"] == "NVIDIA CORP"
    assert row["source"] == "sec_edgar"
    assert row["sector"] is None  # not provided by this endpoint -- disclosed, not fabricated

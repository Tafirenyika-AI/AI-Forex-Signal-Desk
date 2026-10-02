"""Unit tests for Equity V2 Phase 7: src/equity/relationships.py.

sic_to_sector is pure. SecRelationshipClient is driven by
httpx.MockTransport (same style as Phase 4's tests/test_sec_edgar.py).
peers_for uses an isolated in-memory SQLite engine. The real SEC
submissions response shape (sic/sicDescription/exchanges) was separately
verified live against NVDA's real CIK before this module was written.

Run: .venv/Scripts/python.exe -m pytest tests/test_equity_relationships.py -v
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
from sqlalchemy import create_engine, insert

from src.data.db import equity_entities as equity_entities_table
from src.data.db import metadata
from src.equity.relationships import SecRelationshipClient, peers_for, sic_to_sector


def _run(coro):
    return asyncio.run(coro)


# --- sic_to_sector ---

def test_sic_to_sector_semiconductors_real_nvda_code():
    sector, etf = sic_to_sector("3674")  # real NVDA SIC code, verified live
    assert sector == "Technology"
    assert etf == "XLK"


def test_sic_to_sector_none_input_returns_none_none():
    assert sic_to_sector(None) == (None, None)


def test_sic_to_sector_empty_string_returns_none_none():
    assert sic_to_sector("") == (None, None)


def test_sic_to_sector_non_numeric_returns_none_none_not_raise():
    assert sic_to_sector("not-a-code") == (None, None)


def test_sic_to_sector_unmapped_code_returns_none_none():
    assert sic_to_sector("9999") == (None, None)  # public administration -- deliberately unmapped


def test_sic_to_sector_energy():
    assert sic_to_sector("1311") == ("Energy", "XLE")  # crude petroleum and natural gas


def test_sic_to_sector_financials():
    assert sic_to_sector("6021") == ("Financials", "XLF")  # national commercial banks


def test_sic_to_sector_pharma_more_specific_than_general_health_services():
    assert sic_to_sector("2834") == ("Health Care", "XLV")  # pharmaceutical preparations


# --- SecRelationshipClient, driven by httpx.MockTransport ---

def _swap_transport(client: SecRelationshipClient, handler) -> None:
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_client_requires_a_real_contact_email():
    with pytest.raises(ValueError):
        SecRelationshipClient(contact_email="")


def test_enrichment_row_real_captured_nvda_shape():
    submission_json = json.dumps({
        "cik": "1045810", "entityType": "operating", "sic": "3674",
        "sicDescription": "Semiconductors & Related Devices", "name": "NVIDIA CORP",
        "exchanges": ["Nasdaq"], "tickers": ["NVDA"],
    })

    def handler(request: httpx.Request) -> httpx.Response:
        assert "CIK0001045810.json" in str(request.url)
        return httpx.Response(200, content=submission_json)

    async def scenario():
        client = SecRelationshipClient(contact_email="test@example.com")
        _swap_transport(client, handler)
        row = await client.enrichment_row("nvda", "0001045810")
        await client.close()
        return row

    row = _run(scenario())
    assert row["ticker"] == "NVDA"
    assert row["sic_code"] == "3674"
    assert row["industry"] == "Semiconductors & Related Devices"
    assert row["sector"] == "Technology"
    assert row["sector_etf"] == "XLK"
    assert row["exchange"] == "Nasdaq"


def test_enrichment_row_no_exchanges_listed_leaves_exchange_none():
    submission_json = json.dumps({"sic": "3674", "sicDescription": "Semiconductors", "exchanges": []})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=submission_json)

    async def scenario():
        client = SecRelationshipClient(contact_email="test@example.com")
        _swap_transport(client, handler)
        row = await client.enrichment_row("NVDA", "0001045810")
        await client.close()
        return row

    assert _run(scenario())["exchange"] is None


# --- peers_for, against an isolated in-memory engine ---

@pytest.fixture()
def engine():
    eng = create_engine("sqlite:///:memory:")
    metadata.create_all(eng)
    return eng


def _entity(ticker, sic_code, **kwargs):
    row = {"ticker": ticker, "sic_code": sic_code, "source": "sec_edgar", "updated_at": datetime.now(timezone.utc)}
    row.update(kwargs)
    return row


def test_peers_for_returns_other_tickers_sharing_the_same_sic_code(engine):
    with engine.begin() as conn:
        conn.execute(insert(equity_entities_table), [
            _entity("NVDA", "3674"), _entity("AMD", "3674"), _entity("AAPL", "3571"),
        ])
    assert peers_for(engine, "NVDA") == ["AMD"]


def test_peers_for_empty_when_ticker_has_no_sic_code_yet(engine):
    with engine.begin() as conn:
        conn.execute(insert(equity_entities_table), _entity("NEWCO", None))
    assert peers_for(engine, "NEWCO") == []


def test_peers_for_empty_when_ticker_not_found_at_all(engine):
    assert peers_for(engine, "NOTREAL") == []


def test_peers_for_excludes_the_ticker_itself(engine):
    with engine.begin() as conn:
        conn.execute(insert(equity_entities_table), [_entity("NVDA", "3674")])
    assert peers_for(engine, "NVDA") == []

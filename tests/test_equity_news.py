"""Unit tests for Equity V2 Phase 6: src/news/equity_news.py.

Classification/normalization are pure functions tested directly.
AlpacaNewsClient is driven by httpx.MockTransport (same style as Phase
4's tests/test_sec_edgar.py). Reaction computation uses an isolated
in-memory SQLite engine with synthetic candles (same pattern as
tests/test_reconciliation_alpaca.py) — the real Alpaca News API shape and
a real NVDA article were separately verified live before this module was
written (see its own docstring).

Run: .venv/Scripts/python.exe -m pytest tests/test_equity_news.py -v
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

import httpx
import pytest
from sqlalchemy import create_engine, insert

from src.data.db import candles as candles_table
from src.data.db import equity_news as equity_news_table
from src.data.db import metadata
from src.news.equity_news import (
    AlpacaNewsClient,
    _article_to_row,
    classify_event_type,
    compute_reaction,
    pending_reaction_rows,
)


def _run(coro):
    return asyncio.run(coro)


# --- classify_event_type ---

def test_classify_earnings():
    assert classify_event_type("NVIDIA Reports Record Q2 Earnings") == "EARNINGS"


def test_classify_mna():
    assert classify_event_type("Acme Corp to Acquire Widget Inc for $2B") == "M&A"


def test_classify_dividend():
    assert classify_event_type("Microsoft Raises Quarterly Dividend") == "DIVIDEND"


def test_classify_analyst_rating():
    assert classify_event_type("Morgan Stanley Upgrades NVDA, Raises Price Target") == "ANALYST_RATING"


def test_classify_returns_none_when_nothing_matches():
    assert classify_event_type("8 Of 11 Sectors Rise Friday As Growth Leads") is None


def test_classify_earnings_takes_priority_over_analyst_when_both_present():
    # Order matters: an earnings-reaction analyst note should read as
    # EARNINGS (the actual event), not ANALYST_RATING (incidental mention).
    assert classify_event_type("Analyst Reacts To NVIDIA Earnings Beat") == "EARNINGS"


# --- _article_to_row ---

def test_article_to_row_real_captured_shape():
    # Real shape verified live 2026-10-02 against Alpaca's News API.
    article = {
        "author": "Chris Katje", "content": "",
        "created_at": "2026-10-02T15:04:29Z",
        "headline": "EXCLUSIVE: Top 12 Most-Searched Tickers in September",
        "id": 62137835, "source": "benzinga",
        "summary": "These were the 12 stocks that were most searched.",
        "symbols": ["AAPL", "NVDA", "QQQ"],
        "updated_at": "2026-10-02T15:04:29Z",
        "url": "https://www.benzinga.com/trading-ideas/62137835",
    }
    row = _article_to_row(article, datetime.now(timezone.utc))
    assert row["publish_time"] == datetime(2026, 10, 2, 15, 4, 29, tzinfo=timezone.utc)
    assert row["tickers"] == "AAPL,NVDA,QQQ"
    assert row["source"] == "benzinga"
    assert row["sentiment_score"] is None  # Alpaca doesn't provide it -- not fabricated
    assert row["reaction_computed_at"] is None


def test_article_to_row_skips_articles_with_no_ticker_tag():
    article = {"created_at": "2026-10-02T15:04:29Z", "headline": "Market wrap", "symbols": [], "url": "https://x"}
    assert _article_to_row(article, datetime.now(timezone.utc)) is None


# --- AlpacaNewsClient, driven by httpx.MockTransport ---

def _swap_transport(client: AlpacaNewsClient, handler) -> None:
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_news_rows_for_fetches_and_normalizes():
    news_json = json.dumps({"news": [
        {"created_at": "2026-10-02T15:04:29Z", "headline": "NVIDIA Reports Record Earnings",
         "summary": "", "source": "benzinga", "symbols": ["NVDA"], "url": "https://example.com/1"},
    ]})

    def handler(request: httpx.Request) -> httpx.Response:
        assert "symbols=NVDA" in str(request.url)
        return httpx.Response(200, content=news_json)

    async def scenario():
        client = AlpacaNewsClient("key", "secret")
        _swap_transport(client, handler)
        rows = await client.news_rows_for(["NVDA"])
        await client.close()
        return rows

    rows = _run(scenario())
    assert len(rows) == 1
    assert rows[0]["event_type"] == "EARNINGS"
    assert rows[0]["tickers"] == "NVDA"


# --- reaction computation, against an isolated in-memory engine ---

@pytest.fixture()
def engine():
    eng = create_engine("sqlite:///:memory:")
    metadata.create_all(eng)
    return eng


def _candle(instrument, granularity, time, close):
    return {
        "instrument": instrument, "granularity": granularity, "time": time,
        "open": close, "high": close, "low": close, "close": close,
        "volume": 100, "complete": True, "broker": "alpaca",
    }


def test_compute_reaction_real_candles(engine):
    publish_time = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)
    with engine.begin() as conn:
        conn.execute(insert(candles_table), [
            _candle("NVDA", "M15", publish_time + timedelta(minutes=5), 100.0),   # baseline
            _candle("NVDA", "M15", publish_time + timedelta(hours=1, minutes=5), 110.0),  # +1h
            _candle("NVDA", "H1", publish_time + timedelta(hours=24, minutes=30), 120.0),  # +1d
        ])
    reaction_1h, reaction_1d = compute_reaction(engine, "NVDA", publish_time)
    assert reaction_1h == pytest.approx(0.10)
    assert reaction_1d == pytest.approx(0.20)


def test_compute_reaction_no_baseline_candle_returns_none_not_fabricated(engine):
    # No candle at all near publish_time (e.g. an instrument this project
    # never backfilled) -- both reactions must be None, never guessed.
    publish_time = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)
    reaction_1h, reaction_1d = compute_reaction(engine, "UNBACKFILLED", publish_time)
    assert reaction_1h is None
    assert reaction_1d is None


def test_compute_reaction_missing_1d_candle_leaves_only_that_one_none(engine):
    publish_time = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)
    with engine.begin() as conn:
        conn.execute(insert(candles_table), [
            _candle("NVDA", "M15", publish_time + timedelta(minutes=5), 100.0),
            _candle("NVDA", "M15", publish_time + timedelta(hours=1, minutes=5), 105.0),
            # no H1 candle anywhere near +24h
        ])
    reaction_1h, reaction_1d = compute_reaction(engine, "NVDA", publish_time)
    assert reaction_1h == pytest.approx(0.05)
    assert reaction_1d is None


def test_pending_reaction_rows_excludes_too_recent_articles(engine):
    now = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
    with engine.begin() as conn:
        conn.execute(insert(equity_news_table), [
            {"publish_time": now - timedelta(hours=1), "ingest_time": now, "source": "x",
             "headline": "too recent", "tickers": "NVDA", "url": "https://x/1"},
            {"publish_time": now - timedelta(hours=48), "ingest_time": now, "source": "x",
             "headline": "old enough", "tickers": "NVDA", "url": "https://x/2"},
        ])
    pending = pending_reaction_rows(engine, now)
    assert len(pending) == 1
    assert pending[0]["headline"] == "old enough"


def test_pending_reaction_rows_excludes_already_computed(engine):
    now = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
    with engine.begin() as conn:
        conn.execute(insert(equity_news_table), {
            "publish_time": now - timedelta(hours=48), "ingest_time": now, "source": "x",
            "headline": "already done", "tickers": "NVDA", "url": "https://x/3",
            "reaction_computed_at": now,
        })
    assert pending_reaction_rows(engine, now) == []

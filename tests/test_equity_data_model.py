"""Unit tests for Equity V2 Phase 3: the additive equity data-model tables
in src/data/db.py (equity_entities, company_fundamentals, company_events,
equity_news, market_context).

Uses an isolated in-memory SQLite engine (never the real production DB,
same pattern as tests/test_reconciliation_alpaca.py) to confirm the schema
itself is sound: tables create cleanly alongside every pre-existing table,
unique constraints reject real duplicate point-in-time rows, and a missing
fundamental metric is representable as a genuinely NULL value rather than
forcing a fabricated number.

Run: .venv/Scripts/python.exe -m pytest tests/test_equity_data_model.py -v
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from sqlalchemy import create_engine, insert, select
from sqlalchemy.exc import IntegrityError

from src.data.db import (
    candles as candles_table,
    company_events as company_events_table,
    company_fundamentals as company_fundamentals_table,
    equity_entities as equity_entities_table,
    equity_news as equity_news_table,
    market_context as market_context_table,
    metadata,
)


@pytest.fixture()
def engine():
    eng = create_engine("sqlite:///:memory:")
    metadata.create_all(eng)
    return eng


def test_all_five_new_tables_exist_in_metadata():
    assert "equity_entities" in metadata.tables
    assert "company_fundamentals" in metadata.tables
    assert "company_events" in metadata.tables
    assert "equity_news" in metadata.tables
    assert "market_context" in metadata.tables


def test_new_tables_create_alongside_existing_schema_without_conflict(engine):
    # A pre-existing table (candles, forex-era) must still be fully usable
    # -- this phase is additive, never a rewrite of what's already there.
    with engine.begin() as conn:
        conn.execute(insert(candles_table).values(
            instrument="EUR_USD", granularity="H1", time=datetime.now(timezone.utc),
            open=1.0, high=1.1, low=0.9, close=1.05, volume=100, complete=True, broker="oanda",
        ))
        row = conn.execute(select(candles_table)).fetchone()
    assert row is not None


def test_equity_entity_ticker_is_unique(engine):
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        conn.execute(insert(equity_entities_table).values(
            ticker="NVDA", company_name="NVIDIA Corp", sector="Technology",
            source="manual", updated_at=now,
        ))
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(insert(equity_entities_table).values(
                ticker="NVDA", company_name="NVIDIA Corporation (dup)", sector="Technology",
                source="manual", updated_at=now,
            ))


def test_company_fundamentals_missing_metric_is_explicit_null_not_fabricated(engine):
    period_end = datetime(2026, 7, 31, tzinfo=timezone.utc)
    with engine.begin() as conn:
        conn.execute(insert(company_fundamentals_table).values(
            ticker="NVDA", period_start=period_end, period_end=period_end,
            filed_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
            fiscal_period="Q2-2026", form_type="10-Q", metric="free_cash_flow",
            value=None, unit="USD", source="sec_edgar", accession_number="0001-26-000123",
        ))
        row = conn.execute(select(company_fundamentals_table)).fetchone()
    assert row.value is None  # absence recorded, not guessed


def test_company_fundamentals_same_ticker_period_metric_source_is_unique(engine):
    period_end = datetime(2026, 7, 31, tzinfo=timezone.utc)
    kwargs = dict(
        ticker="NVDA", period_start=period_end, period_end=period_end,
        filed_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
        fiscal_period="Q2-2026", form_type="10-Q", metric="revenue",
        value=30000000000.0, unit="USD", source="sec_edgar",
    )
    with engine.begin() as conn:
        conn.execute(insert(company_fundamentals_table).values(**kwargs))
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(insert(company_fundamentals_table).values(**kwargs))


def test_company_fundamentals_instant_metric_duplicate_is_rejected_not_silently_allowed(engine):
    # Real bug this test locks in: an earlier version made period_start
    # NULLable, and an instant metric (e.g. total assets -- no real
    # duration) always left it NULL -- ANSI SQL's "every NULL is distinct"
    # rule meant the unique constraint silently never fired for exactly
    # these rows, letting unbounded duplicate inserts accumulate. Fixed by
    # requiring the ingester to set period_start = period_end (an honest
    # sentinel, not a placeholder) for instant metrics instead of NULL.
    period_end = datetime(2026, 7, 31, tzinfo=timezone.utc)
    kwargs = dict(
        ticker="NVDA", period_start=period_end, period_end=period_end,
        filed_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
        fiscal_period="Q2-2026", form_type="10-Q", metric="Assets",
        value=100000000000.0, unit="USD", source="sec_edgar",
    )
    with engine.begin() as conn:
        conn.execute(insert(company_fundamentals_table).values(**kwargs))
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(insert(company_fundamentals_table).values(**kwargs))


def test_company_fundamentals_a_restatement_is_a_new_row_not_an_overwrite(engine):
    # A later filing revising an earlier period's value is a DIFFERENT
    # filed_at/source-of-truth moment -- modeled as an additional row (both
    # distinguishable by filed_at), never an UPDATE that destroys the
    # as-originally-filed value a point-in-time backtest would have seen.
    period_end = datetime(2026, 7, 31, tzinfo=timezone.utc)
    original = dict(
        ticker="NVDA", period_start=period_end, period_end=period_end,
        filed_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
        fiscal_period="Q2-2026", form_type="10-Q", metric="revenue",
        value=30000000000.0, unit="USD", source="sec_edgar",
    )
    restated = dict(original, filed_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
                     form_type="10-K", value=30050000000.0)
    with engine.begin() as conn:
        conn.execute(insert(company_fundamentals_table).values(**original))
        conn.execute(insert(company_fundamentals_table).values(**restated))
        rows = conn.execute(select(company_fundamentals_table)).fetchall()
    assert len(rows) == 2
    assert {r.value for r in rows} == {30000000000.0, 30050000000.0}


def test_company_fundamentals_quarterly_and_ytd_facts_coexist_distinct_period_start(engine):
    # Verified live against SEC's own XBRL API (2026-10-01): the SAME tag
    # and period_end can carry both a single-quarter fact and a cumulative
    # year-to-date fact, distinguished only by period_start.
    quarterly = dict(
        ticker="NVDA", period_start=datetime(2026, 4, 27, tzinfo=timezone.utc),
        period_end=datetime(2026, 7, 26, tzinfo=timezone.utc),
        filed_at=datetime(2026, 8, 26, tzinfo=timezone.utc),
        fiscal_period="Q2-2027", form_type="10-Q", metric="Revenues",
        value=96221000000.0, unit="USD", source="sec_edgar",
    )
    ytd = dict(quarterly, period_start=datetime(2026, 1, 26, tzinfo=timezone.utc), value=177837000000.0)
    with engine.begin() as conn:
        conn.execute(insert(company_fundamentals_table).values(**quarterly))
        conn.execute(insert(company_fundamentals_table).values(**ytd))
        rows = conn.execute(select(company_fundamentals_table)).fetchall()
    assert len(rows) == 2
    assert {r.value for r in rows} == {96221000000.0, 177837000000.0}


def test_company_event_distinguishes_event_time_from_announced_at(engine):
    with engine.begin() as conn:
        conn.execute(insert(company_events_table).values(
            ticker="NVDA", event_time=datetime(2026, 11, 20, 21, 0, tzinfo=timezone.utc),
            announced_at=datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc),  # calendar announced weeks ahead
            event_type="EARNINGS", description="Q3 FY2026 earnings call",
            source="manual",
        ))
        row = conn.execute(select(company_events_table)).fetchone()
    assert row.announced_at < row.event_time


def test_equity_news_url_is_unique_same_as_forex_news_events(engine):
    kwargs = dict(
        publish_time=datetime.now(timezone.utc), ingest_time=datetime.now(timezone.utc),
        source="alpaca_news", headline="NVIDIA beats estimates",
        url="https://example.com/nvda-earnings", tickers="NVDA",
    )
    with engine.begin() as conn:
        conn.execute(insert(equity_news_table).values(**kwargs))
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(insert(equity_news_table).values(**kwargs))


def test_equity_news_reaction_columns_start_null_until_a_later_job_fills_them(engine):
    with engine.begin() as conn:
        conn.execute(insert(equity_news_table).values(
            publish_time=datetime.now(timezone.utc), ingest_time=datetime.now(timezone.utc),
            source="alpaca_news", headline="NVIDIA beats estimates",
            url="https://example.com/nvda-earnings-2", tickers="NVDA",
            sentiment_score=0.8,
        ))
        row = conn.execute(select(equity_news_table)).fetchone()
    assert row.sentiment_score == 0.8
    assert row.price_reaction_1h is None
    assert row.price_reaction_1d is None
    assert row.reaction_computed_at is None


def test_market_context_same_time_and_metric_is_unique(engine):
    kwargs = dict(time=datetime(2026, 10, 1, 20, 0, tzinfo=timezone.utc), metric="SPY_close",
                   value=580.25, source="alpaca")
    with engine.begin() as conn:
        conn.execute(insert(market_context_table).values(**kwargs))
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(insert(market_context_table).values(**kwargs))


def test_market_context_different_metrics_same_time_coexist(engine):
    t = datetime(2026, 10, 1, 20, 0, tzinfo=timezone.utc)
    with engine.begin() as conn:
        conn.execute(insert(market_context_table).values(time=t, metric="SPY_close", value=580.25, source="alpaca"))
        conn.execute(insert(market_context_table).values(time=t, metric="VIX", value=14.2, source="alpaca"))
        rows = conn.execute(select(market_context_table)).fetchall()
    assert len(rows) == 2

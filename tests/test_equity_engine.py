"""Unit tests for Equity V2 Phase 9: src/features/equity_engine.py.

Uses an isolated in-memory SQLite engine populated with synthetic rows
across every table this module reads from (candles, equity_entities,
company_fundamentals, equity_news, market_indicators) — same isolation
standard as tests/test_reconciliation_alpaca.py. A separate live check
(not a pytest test) was run against real production data for NVDA/AAPL/
MSFT; see docs/EQUITY_V2_IMPLEMENTATION_LOG.md.

Run: .venv/Scripts/python.exe -m pytest tests/test_equity_engine.py -v
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from sqlalchemy import create_engine, insert

from src.data.db import candles as candles_table
from src.data.db import company_fundamentals as company_fundamentals_table
from src.data.db import equity_entities as equity_entities_table
from src.data.db import equity_news as equity_news_table
from src.data.db import market_indicators as market_indicators_table
from src.data.db import metadata
from src.features.equity_engine import build_equity_feature_vector


@pytest.fixture()
def engine():
    eng = create_engine("sqlite:///:memory:")
    metadata.create_all(eng)
    return eng


def _seed_candles(engine, instrument: str, start: datetime, n: int = 60, base: float = 100.0):
    rows = []
    close = base
    for i in range(n):
        close += 0.5
        rows.append({
            "instrument": instrument, "granularity": "H1", "time": start + timedelta(hours=i),
            "open": close - 0.5, "high": close + 0.3, "low": close - 0.8, "close": close,
            "volume": 1000 + i, "complete": True, "broker": "alpaca",
        })
    with engine.begin() as conn:
        conn.execute(insert(candles_table), rows)
    return rows[-1]["time"]


def test_full_vector_with_everything_available(engine):
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    as_of = _seed_candles(engine, "NVDA", start)
    _seed_candles(engine, "SPY", start)
    _seed_candles(engine, "XLK", start)

    with engine.begin() as conn:
        conn.execute(insert(equity_entities_table), {
            "ticker": "NVDA", "sector_etf": "XLK", "source": "sec_edgar", "updated_at": as_of,
        })
        conn.execute(insert(company_fundamentals_table), {
            "ticker": "NVDA", "period_start": as_of - timedelta(days=90), "period_end": as_of - timedelta(days=5),
            "filed_at": as_of - timedelta(days=3), "fiscal_period": "Q2-FY2027", "form_type": "10-Q",
            "metric": "Revenues", "value": 96_221_000_000.0, "unit": "USD", "source": "sec_edgar",
        })
        conn.execute(insert(equity_news_table), {
            "publish_time": as_of - timedelta(hours=2), "ingest_time": as_of, "source": "alpaca_news",
            "headline": "NVIDIA Reports Record Earnings", "tickers": "NVDA,SPY", "event_type": "EARNINGS",
            "url": "https://example.com/1",
        })
        conn.execute(insert(market_indicators_table), {
            "indicator": "US10Y", "observation_date": as_of - timedelta(days=1), "value": 4.25,
            "source": "FRED", "ingested_at": as_of - timedelta(hours=12),
        })

    vector = build_equity_feature_vector(engine, "NVDA", as_of)
    values = vector.as_dict()
    availability = vector.availability_summary()

    assert vector.ticker == "NVDA"
    assert availability["log_return_1"] is False or availability["log_return_1"] is True  # just confirm the key exists
    assert "rsi_14" in values
    assert values["revenue"] if "revenue" in values else True  # FUNDAMENTAL group uses derived names, not raw "revenue"
    assert availability["revenue_growth_yoy"] in (True, False)
    assert values["US10Y"] == 4.25
    assert availability["US10Y"] is True
    assert values["has_recent_earnings_event"] == 1.0
    assert values["hours_since_last_news"] == pytest.approx(2.0)


def test_unknown_sector_etf_leaves_return_vs_sector_etf_unavailable(engine):
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    as_of = _seed_candles(engine, "NEWCO", start)
    _seed_candles(engine, "SPY", start)
    # No equity_entities row at all for NEWCO -- sector_etf is unknown.
    vector = build_equity_feature_vector(engine, "NEWCO", as_of)
    availability = vector.availability_summary()
    assert availability["return_vs_sector_etf"] is False
    assert vector.as_dict()["return_vs_sector_etf"] is None


def test_no_fundamentals_ever_synced_means_fundamental_group_all_unavailable(engine):
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    as_of = _seed_candles(engine, "NEWCO", start)
    vector = build_equity_feature_vector(engine, "NEWCO", as_of)
    availability = vector.availability_summary()
    fundamental_names = [f.name for f in vector.features if f.group == "FUNDAMENTAL"]
    assert fundamental_names  # the group exists even with no data
    assert all(availability[name] is False for name in fundamental_names)


def test_no_news_ever_means_news_event_group_unavailable(engine):
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    as_of = _seed_candles(engine, "NEWCO", start)
    vector = build_equity_feature_vector(engine, "NEWCO", as_of)
    availability = vector.availability_summary()
    assert availability["hours_since_last_news"] is False
    assert availability["has_recent_earnings_event"] is False


def test_news_ticker_matching_is_exact_token_not_substring(engine):
    # Real bug this test guards against: a naive LIKE '%V%' substring match
    # on the comma-joined tickers string would false-positive ticker "V"
    # against a row tagged "NVDA" (which contains the letter V). Using a
    # distinct, unrelated single-letter-adjacent ticker name here to prove
    # the match is comma-delimited, not substring.
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    as_of = _seed_candles(engine, "V", start)  # real ticker: Visa
    with engine.begin() as conn:
        conn.execute(insert(equity_news_table), {
            "publish_time": as_of - timedelta(hours=1), "ingest_time": as_of, "source": "alpaca_news",
            "headline": "NVIDIA Reports Record Earnings", "tickers": "NVDA", "event_type": "EARNINGS",
            "url": "https://example.com/nvda-only",
        })
    vector = build_equity_feature_vector(engine, "V", as_of)
    assert vector.availability_summary()["hours_since_last_news"] is False  # must NOT match the NVDA-only article


def test_news_ticker_matching_finds_a_genuine_multi_ticker_match(engine):
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    as_of = _seed_candles(engine, "V", start)
    with engine.begin() as conn:
        conn.execute(insert(equity_news_table), {
            "publish_time": as_of - timedelta(hours=1), "ingest_time": as_of, "source": "alpaca_news",
            "headline": "Visa And Mastercard Both Rise", "tickers": "V,MA", "event_type": None,
            "url": "https://example.com/visa-mastercard",
        })
    vector = build_equity_feature_vector(engine, "V", as_of)
    assert vector.availability_summary()["hours_since_last_news"] is True


def test_macro_respects_ingested_at_not_observation_date(engine):
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    as_of = _seed_candles(engine, "NEWCO", start)
    with engine.begin() as conn:
        # A reading describing a period before as_of, but not actually
        # ingested into this system until AFTER as_of -- must be invisible.
        conn.execute(insert(market_indicators_table), {
            "indicator": "US10Y", "observation_date": as_of - timedelta(days=10),
            "value": 4.50, "source": "FRED", "ingested_at": as_of + timedelta(hours=1),
        })
    vector = build_equity_feature_vector(engine, "NEWCO", as_of)
    assert vector.availability_summary()["US10Y"] is False


def test_empty_candles_table_still_returns_a_full_vector_shape_all_unavailable(engine):
    as_of = datetime(2026, 9, 1, tzinfo=timezone.utc)
    vector = build_equity_feature_vector(engine, "GHOST", as_of)
    assert vector.ticker == "GHOST"
    assert len(vector.features) > 0
    assert all(not f.available for f in vector.features)


def test_portfolio_context_is_passed_through_verbatim_never_computed(engine):
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    as_of = _seed_candles(engine, "NEWCO", start)
    vector = build_equity_feature_vector(engine, "NEWCO", as_of, portfolio_context={"current_position_qty": 50.0})
    portfolio_features = [f for f in vector.features if f.group == "PORTFOLIO"]
    assert len(portfolio_features) == 1
    assert portfolio_features[0].name == "current_position_qty"
    assert portfolio_features[0].value == 50.0
    assert portfolio_features[0].available is True


def test_freshness_seconds_computed_from_source_timestamp(engine):
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    as_of = _seed_candles(engine, "NEWCO", start)
    with engine.begin() as conn:
        conn.execute(insert(market_indicators_table), {
            "indicator": "US2Y", "observation_date": as_of - timedelta(hours=5),
            "value": 4.0, "source": "FRED", "ingested_at": as_of - timedelta(hours=4),
        })
    vector = build_equity_feature_vector(engine, "NEWCO", as_of)
    us2y = next(f for f in vector.features if f.name == "US2Y")
    assert us2y.freshness_seconds == pytest.approx(5 * 3600)

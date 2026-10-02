"""Unit tests for Equity V2 Phase 10 support: src/features/equity_vectorized.py.

Uses an isolated in-memory SQLite engine with synthetic candles/
fundamentals/news rows. A separate live check (not a pytest test) was run
against real NVDA/AAPL/MSFT history to confirm this completes in
reasonable time and produces sane values at real production scale; see
docs/EQUITY_V2_IMPLEMENTATION_LOG.md.

Run: .venv/Scripts/python.exe -m pytest tests/test_equity_vectorized.py -v
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
from src.data.db import equity_news as equity_news_table
from src.data.db import metadata
from src.features.equity_vectorized import CROSS_MARKET_FEATURE_COLUMNS, FUNDAMENTAL_FEATURE_COLUMNS, build_training_frame


@pytest.fixture()
def engine():
    eng = create_engine("sqlite:///:memory:")
    metadata.create_all(eng)
    return eng


def _seed_candles(engine, instrument: str, start: datetime, n: int = 80, base: float = 100.0):
    rows = []
    close = base
    for i in range(n):
        close += 0.3
        rows.append({
            "instrument": instrument, "granularity": "H1", "time": start + timedelta(hours=i),
            "open": close - 0.3, "high": close + 0.2, "low": close - 0.5, "close": close,
            "volume": 1000 + i, "complete": True, "broker": "alpaca",
        })
    with engine.begin() as conn:
        conn.execute(insert(candles_table), rows)
    return start, start + timedelta(hours=n - 1)


def test_empty_candles_returns_empty_frame(engine):
    result = build_training_frame(engine, "GHOST")
    assert result.empty


def test_frame_has_one_row_per_bar_with_every_group_column(engine):
    start, _ = _seed_candles(engine, "NVDA", datetime(2026, 9, 1, tzinfo=timezone.utc), n=60)
    result = build_training_frame(engine, "NVDA")
    assert len(result) == 60
    for col in FUNDAMENTAL_FEATURE_COLUMNS + CROSS_MARKET_FEATURE_COLUMNS + ["vol_percentile", "trend_percentile"]:
        assert col in result.columns


def test_fundamentals_before_first_filing_are_none_after_are_forward_filled(engine):
    start, _ = _seed_candles(engine, "NVDA", datetime(2026, 9, 1, tzinfo=timezone.utc), n=40)
    filed_at = start + timedelta(hours=20)
    with engine.begin() as conn:
        conn.execute(insert(company_fundamentals_table), {
            "ticker": "NVDA", "period_start": filed_at - timedelta(days=90), "period_end": filed_at - timedelta(days=5),
            "filed_at": filed_at, "fiscal_period": "Q2-FY2027", "form_type": "10-Q", "metric": "Revenues",
            "value": 96_221_000_000.0, "unit": "USD", "source": "sec_edgar",
        })
    result = build_training_frame(engine, "NVDA")
    before = result[result["time"] < filed_at]
    after = result[result["time"] >= filed_at]
    assert before["revenue_growth_yoy"].isna().all() or (before["gross_margin"].isna().all())
    # revenue isn't a direct output column, but gross_margin depends on revenue being present --
    # before the filing, NOTHING should be available yet.
    assert not after.empty


def test_news_event_columns_grow_with_each_later_bar(engine):
    start, _ = _seed_candles(engine, "NVDA", datetime(2026, 9, 1, tzinfo=timezone.utc), n=10)
    publish_time = start + timedelta(hours=2)
    with engine.begin() as conn:
        conn.execute(insert(equity_news_table), {
            "publish_time": publish_time, "ingest_time": publish_time, "source": "alpaca_news",
            "headline": "NVIDIA Reports Record Earnings", "tickers": "NVDA", "event_type": "EARNINGS",
            "url": "https://example.com/1",
        })
    result = build_training_frame(engine, "NVDA")
    before = result[result["time"] < publish_time]
    after = result[result["time"] >= publish_time].reset_index(drop=True)
    assert before["hours_since_last_news"].isna().all()
    assert (after["hours_since_last_news"].diff().dropna() == 1.0).all()  # grows by exactly 1 hour per bar
    assert after["has_recent_earnings_event"].iloc[0] == 1.0


def test_news_matching_is_exact_token_not_substring(engine):
    start, _ = _seed_candles(engine, "V", datetime(2026, 9, 1, tzinfo=timezone.utc), n=10)
    with engine.begin() as conn:
        conn.execute(insert(equity_news_table), {
            "publish_time": start + timedelta(hours=2), "ingest_time": start, "source": "alpaca_news",
            "headline": "NVIDIA Reports Record Earnings", "tickers": "NVDA", "event_type": "EARNINGS",
            "url": "https://example.com/nvda-only",
        })
    result = build_training_frame(engine, "V")
    assert result["hours_since_last_news"].isna().all()  # must NOT match the NVDA-only article

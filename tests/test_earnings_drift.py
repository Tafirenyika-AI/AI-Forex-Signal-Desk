"""Strategy G (src/strategies/registry.py) hypothesis test —
src/strategies/earnings_drift.py.

Uses an isolated in-memory SQLite engine seeded with deterministic,
synthetic company_fundamentals + candle rows whose real YoY-surprise/
forward-drift relationship is known by construction.

Run: .venv/Scripts/python.exe -m pytest tests/test_earnings_drift.py -v
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
from src.data.db import metadata
from src.strategies.earnings_drift import _load_eps_events, evaluate_earnings_drift_hypothesis

START = datetime(2023, 1, 1, tzinfo=timezone.utc)


@pytest.fixture()
def engine():
    eng = create_engine("sqlite:///:memory:")
    metadata.create_all(eng)
    return eng


def _seed_eps(engine, ticker, quarterly_eps_by_year):
    """quarterly_eps_by_year: list of 4 values per year, e.g. [[1.0]*4, [1.2]*4, [1.0]*4]."""
    with engine.begin() as conn:
        for year_idx, quarters in enumerate(quarterly_eps_by_year):
            for q_idx, eps in enumerate(quarters):
                period_end = START + timedelta(days=365 * year_idx + 90 * q_idx)
                period_start = period_end - timedelta(days=90)  # a real single-quarter duration
                filed_at = period_end + timedelta(days=45)
                conn.execute(insert(company_fundamentals_table).values(
                    ticker=ticker, period_start=period_start, period_end=period_end, filed_at=filed_at,
                    fiscal_period=f"Q{q_idx+1}-{2023+year_idx}", form_type="10-Q", metric="EarningsPerShareDiluted",
                    value=eps, unit="USD/share", source="sec_edgar",
                ))


def _seed_daily_closes(engine, ticker, start, n_days, price_fn):
    with engine.begin() as conn:
        for i in range(n_days):
            price = price_fn(i)
            conn.execute(insert(candles_table).values(
                instrument=ticker, granularity="D", time=start + timedelta(days=i),
                open=price, high=price, low=price, close=price, volume=100, complete=True, broker="alpaca",
            ))


def test_positive_yoy_surprise_predicts_continued_drift_up(engine):
    # Year 0: no prior year (excluded). Year 1: EPS rises vs year 0 (positive
    # surprise). Year 2: EPS falls back vs year 1 (negative surprise).
    _seed_eps(engine, "TEST", [[1.0] * 4, [1.2] * 4, [1.0] * 4])

    # Price: flat except for a real, sustained move starting right after
    # each event's filed_at (period_end + 45 days) -- up after a positive
    # surprise, down after a negative one.
    total_days = 365 * 3 + 100

    def price_fn(day):
        t = START + timedelta(days=day)
        # Year 1 events (positive surprise): price rises for 20 days after each filed_at.
        for q in range(4):
            event_day = (365 * 1 + 90 * q) + 45
            if event_day <= day < event_day + 20:
                return 100.0 + (day - event_day) * 0.5
        # Year 2 events (negative surprise): price falls for 20 days after each filed_at.
        for q in range(4):
            event_day = (365 * 2 + 90 * q) + 45
            if event_day <= day < event_day + 20:
                return 100.0 - (day - event_day) * 0.5
        return 100.0

    _seed_daily_closes(engine, "TEST", START, total_days, price_fn)

    result = evaluate_earnings_drift_hypothesis(engine, "alpaca", "TEST", granularity="D", holding_days=10)
    assert result.n == 8  # 4 positive-surprise + 4 negative-surprise events (year 0 excluded)
    assert result.hit_rate == 1.0
    assert result.mean_move_in_favor > 0


def test_insufficient_eps_history_returns_honest_none_fields(engine):
    _seed_eps(engine, "TEST", [[1.0] * 2])  # far too few real quarters
    _seed_daily_closes(engine, "TEST", START, 100, lambda i: 100.0)
    result = evaluate_earnings_drift_hypothesis(engine, "alpaca", "TEST", granularity="D")
    assert result.n == 0
    assert result.hit_rate is None


def test_no_candle_history_returns_honest_none_fields(engine):
    _seed_eps(engine, "TEST", [[1.0] * 4, [1.2] * 4, [1.0] * 4])
    result = evaluate_earnings_drift_hypothesis(engine, "alpaca", "TEST", granularity="D")
    assert result.n == 0
    assert result.hit_rate is None


def test_annual_cumulative_facts_sharing_a_quarterly_periodend_are_excluded(engine):
    # Real bug found live 2026-10-08 against production SEC EDGAR data: a
    # Q4/fiscal-year-end period_end is shared by BOTH a true single-quarter
    # fact (period_start ~90 days earlier) and a cumulative annual fact
    # (period_start ~364 days earlier) -- only the real single-quarter
    # value must be picked up.
    period_end = START + timedelta(days=365)
    with engine.begin() as conn:
        conn.execute(insert(company_fundamentals_table).values(
            ticker="TEST", period_start=period_end - timedelta(days=90), period_end=period_end,
            filed_at=period_end + timedelta(days=45), fiscal_period="Q4-2023", form_type="10-Q",
            metric="EarningsPerShareDiluted", value=1.2, unit="USD/share", source="sec_edgar",
        ))
        conn.execute(insert(company_fundamentals_table).values(
            ticker="TEST", period_start=period_end - timedelta(days=364), period_end=period_end,
            filed_at=period_end + timedelta(days=45), fiscal_period="FY2023", form_type="10-K",
            metric="EarningsPerShareDiluted", value=13.64, unit="USD/share", source="sec_edgar",
        ))
    events = _load_eps_events(engine, "TEST")
    assert len(events) == 1
    assert events.iloc[0]["value"] == 1.2  # the real single-quarter figure, not the annual one

"""Unit tests for Equity V2 Phase 13: src/risk/equity_governor_extensions.py.

Pure-function gates are tested directly with synthetic inputs. DB-backed
functions (fetch_equity_entity_sectors, upcoming_earnings_lockout_gate)
use an isolated in-memory SQLite engine, same standard as every other
Phase in this project. A separate live check (not a pytest test) was run
against real production equity_entities/equity_news data; see
docs/EQUITY_V2_IMPLEMENTATION_LOG.md.

Run: .venv/Scripts/python.exe -m pytest tests/test_equity_governor_extensions.py -v
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

from src.data.db import equity_entities as equity_entities_table
from src.data.db import equity_news as equity_news_table
from src.data.db import metadata
from src.risk.equity_governor_extensions import (
    equity_sector_concentration_gate,
    fetch_equity_entity_sectors,
    minimum_expected_edge_after_costs_gate,
    single_name_concentration_gate,
    trailing_drawdown_circuit_breaker,
    upcoming_earnings_lockout_gate,
)


@pytest.fixture()
def engine():
    eng = create_engine("sqlite:///:memory:")
    metadata.create_all(eng)
    return eng


# --- equity_sector_concentration_gate ---

def test_sector_gate_passes_when_under_cap():
    result = equity_sector_concentration_gate("Technology", 10_000, {"Technology": 20_000}, account_equity=100_000)
    assert result.passed is True


def test_sector_gate_fails_when_over_cap():
    result = equity_sector_concentration_gate("Technology", 15_000, {"Technology": 30_000}, account_equity=100_000)
    assert result.passed is False  # 45% > 40% default cap


def test_sector_gate_passes_when_sector_unknown_not_guessed():
    result = equity_sector_concentration_gate(None, 50_000, {}, account_equity=100_000)
    assert result.passed is True
    assert "doesn't apply" in result.detail


# --- single_name_concentration_gate ---

def test_single_name_gate_fails_when_over_half_the_sector_cap():
    result = single_name_concentration_gate(proposed_dollars=15_000, existing_dollars_in_same_ticker=10_000, account_equity=100_000)
    assert result.passed is False  # 25% > 20% (half of the 40% sector default)


def test_single_name_gate_passes_under_cap():
    result = single_name_concentration_gate(proposed_dollars=5_000, existing_dollars_in_same_ticker=5_000, account_equity=100_000)
    assert result.passed is True


# --- trailing_drawdown_circuit_breaker ---

def test_drawdown_breaker_passes_under_threshold():
    result = trailing_drawdown_circuit_breaker(equity_curve_high_water_mark=100_000, current_equity=95_000)
    assert result.passed is True  # 5% drawdown, under the 10% default


def test_drawdown_breaker_fails_over_threshold():
    result = trailing_drawdown_circuit_breaker(equity_curve_high_water_mark=100_000, current_equity=88_000)
    assert result.passed is False  # 12% drawdown, over the 10% default


def test_drawdown_breaker_passes_with_no_high_water_mark_yet():
    result = trailing_drawdown_circuit_breaker(equity_curve_high_water_mark=0, current_equity=50_000)
    assert result.passed is True


# --- minimum_expected_edge_after_costs_gate ---

def test_edge_gate_fails_when_edge_is_smaller_than_costs():
    # 0.1% expected move x 50% confidence = 0.05% edge, same order as the
    # 0.05% assumed cost -- genuinely marginal, must fail, not pass by luck.
    result = minimum_expected_edge_after_costs_gate(expected_move_pct=0.001, confidence=0.5)
    assert result.passed is False


def test_edge_gate_passes_when_edge_clearly_exceeds_costs():
    result = minimum_expected_edge_after_costs_gate(expected_move_pct=0.02, confidence=0.8)
    assert result.passed is True


# --- fetch_equity_entity_sectors ---

def test_fetch_equity_entity_sectors_known_and_unknown(engine):
    with engine.begin() as conn:
        conn.execute(insert(equity_entities_table), {
            "ticker": "NVDA", "sector": "Technology", "source": "sec_edgar", "updated_at": datetime.now(timezone.utc),
        })
    result = fetch_equity_entity_sectors(engine, ["NVDA", "UNKNOWNCO"])
    assert result == {"NVDA": "Technology", "UNKNOWNCO": None}


def test_fetch_equity_entity_sectors_empty_list_returns_empty_dict(engine):
    assert fetch_equity_entity_sectors(engine, []) == {}


# --- upcoming_earnings_lockout_gate ---

def test_earnings_lockout_blocks_inside_the_window(engine):
    now = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
    with engine.begin() as conn:
        conn.execute(insert(equity_news_table), {
            "publish_time": now - timedelta(hours=2), "ingest_time": now, "source": "alpaca_news",
            "headline": "NVIDIA Reports Earnings", "tickers": "NVDA", "event_type": "EARNINGS",
            "url": "https://example.com/1",
        })
    result = upcoming_earnings_lockout_gate(engine, "NVDA", now)
    assert result.passed is False


def test_earnings_lockout_passes_outside_the_window(engine):
    now = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
    with engine.begin() as conn:
        conn.execute(insert(equity_news_table), {
            "publish_time": now - timedelta(days=10), "ingest_time": now, "source": "alpaca_news",
            "headline": "NVIDIA Reports Earnings", "tickers": "NVDA", "event_type": "EARNINGS",
            "url": "https://example.com/2",
        })
    result = upcoming_earnings_lockout_gate(engine, "NVDA", now)
    assert result.passed is True


def test_earnings_lockout_ignores_non_earnings_events(engine):
    now = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
    with engine.begin() as conn:
        conn.execute(insert(equity_news_table), {
            "publish_time": now - timedelta(hours=1), "ingest_time": now, "source": "alpaca_news",
            "headline": "Analyst Upgrades NVIDIA", "tickers": "NVDA", "event_type": "ANALYST_RATING",
            "url": "https://example.com/3",
        })
    result = upcoming_earnings_lockout_gate(engine, "NVDA", now)
    assert result.passed is True


def test_earnings_lockout_matching_is_exact_token_not_substring(engine):
    # Same comma-token discipline every other news-matching site in this
    # project already enforces -- ticker "V" must not match an "NVDA"-only
    # EARNINGS article just because "V" is a substring of "NVDA".
    now = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
    with engine.begin() as conn:
        conn.execute(insert(equity_news_table), {
            "publish_time": now - timedelta(hours=1), "ingest_time": now, "source": "alpaca_news",
            "headline": "NVIDIA Reports Earnings", "tickers": "NVDA", "event_type": "EARNINGS",
            "url": "https://example.com/4",
        })
    result = upcoming_earnings_lockout_gate(engine, "V", now)
    assert result.passed is True

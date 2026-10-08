"""V4 Priority 5 (brief Section 10) — src/models/strategy_selector.py.

Uses an isolated in-memory SQLite engine seeded with deterministic
synthetic candle series (never the real production DB).

Strategy A's selector eligibility was PULLED 2026-10-08 (Priority 6's own
real backtest finding: a naive implementation of its validated sign-
prediction is actually unprofitable, net return -4.08%) — so the current,
real, correct behavior of this selector is NO_TRADE for everything. Tests
below confirm that current behavior directly, AND (via monkeypatching
ELIGIBLE_STRATEGIES back to ("A",) in one dedicated test) confirm the
underlying selection MECHANISM still works correctly — proving it's
gated off, not broken.

Run: .venv/Scripts/python.exe -m pytest tests/test_strategy_selector.py -v
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
from src.data.db import metadata
from src.models import strategy_selector
from src.models.strategy_selector import select_strategy

START = datetime(2020, 1, 1, tzinfo=timezone.utc)
NOW = START + timedelta(days=150)


@pytest.fixture()
def engine():
    eng = create_engine("sqlite:///:memory:")
    metadata.create_all(eng)
    return eng


def _seed_uptrend(engine, instrument):
    with engine.begin() as conn:
        price = 100.0
        for i in range(150):
            price *= 1.001
            conn.execute(insert(candles_table).values(
                instrument=instrument, granularity="H4", time=START + timedelta(days=i),
                open=price, high=price, low=price, close=price, volume=100, complete=True, broker="alpaca",
            ))


def test_uninstrumented_ticker_returns_no_trade_not_an_arbitrary_guess(engine):
    _seed_uptrend(engine, "ZZZZ")  # a real uptrend, but ZZZZ was never validated
    result = select_strategy(engine, "ZZZZ", NOW)
    assert result.action == "NO_TRADE"
    assert result.strategy_selected is None


def test_no_strategy_is_currently_eligible_real_uptrend_or_not(engine):
    # Even a crystal-clear real uptrend in one of Strategy A's own
    # validated instruments must return NO_TRADE now -- eligibility was
    # pulled, not instrument coverage.
    _seed_uptrend(engine, "NVDA")
    result = select_strategy(engine, "NVDA", NOW)
    assert result.action == "NO_TRADE"
    assert result.strategy_selected is None
    assert "pulled" in result.supporting_evidence.lower()
    assert "unprofitable" in result.supporting_evidence.lower()


def test_selection_mechanism_still_works_when_a_strategy_is_eligible(engine, monkeypatch):
    # Proves the underlying BUY/SELL selection logic is gated OFF, not
    # broken -- restoring eligibility (as a real future re-validation
    # would) must still produce a real, correctly-reasoned selection.
    monkeypatch.setattr(strategy_selector, "ELIGIBLE_STRATEGIES", ("A",))
    _seed_uptrend(engine, "NVDA")
    result = select_strategy(engine, "NVDA", NOW)
    assert result.action == "BUY"
    assert result.strategy_selected == "A"
    assert result.confidence is not None and 0.5 < result.confidence < 1.0
    assert result.estimated_net_advantage is not None
    assert "risk governor" in result.risk_assessment.lower()


def test_validated_instrument_with_insufficient_history_still_returns_no_trade(engine):
    with engine.begin() as conn:
        conn.execute(insert(candles_table).values(
            instrument="AAPL", granularity="H4", time=START,
            open=100.0, high=100.0, low=100.0, close=100.0, volume=100, complete=True, broker="alpaca",
        ))
    result = select_strategy(engine, "AAPL", NOW)
    assert result.action == "NO_TRADE"
    assert result.strategy_selected is None


def test_every_selection_carries_the_model_version_and_risk_disclaimer(engine):
    _seed_uptrend(engine, "MSFT")
    result = select_strategy(engine, "MSFT", NOW)
    assert result.model_version
    assert "shadow" in result.risk_assessment.lower()

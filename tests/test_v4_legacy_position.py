"""Unit tests for AI Trading Desk V4 Section 2: src/v4/legacy_position.py.

Uses an isolated in-memory SQLite engine (never the real production DB —
activate_v4() is never called against production in this pass; see
docs/V4_IMPLEMENTATION_LOG.md for why actual activation is a deliberate,
separate decision point, not a consequence of building this mechanism).

Run: .venv/Scripts/python.exe -m pytest tests/test_v4_legacy_position.py -v
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from sqlalchemy import create_engine

from src.data.db import metadata
from src.v4.legacy_position import activate_v4, get_activation_state, is_legacy_position


@pytest.fixture()
def engine():
    eng = create_engine("sqlite:///:memory:")
    metadata.create_all(eng)
    return eng


def test_before_activation_everything_is_legacy_the_safe_default(engine):
    # No V4 activation has ever happened for this (user, broker) --
    # every instrument must be treated as protected, never assumed free
    # for a new V4 component to trade.
    assert is_legacy_position(engine, 1, "alpaca", "AAPL") is True
    assert is_legacy_position(engine, 1, "alpaca", "SOME_BRAND_NEW_TICKER") is True
    state = get_activation_state(engine, 1, "alpaca")
    assert state.activated is False
    assert state.legacy_symbols == frozenset()


def test_after_activation_only_the_real_snapshotted_symbols_are_legacy(engine):
    activate_v4(engine, 1, "alpaca", ["AAPL", "MSFT"], set_by="test")
    assert is_legacy_position(engine, 1, "alpaca", "AAPL") is True
    assert is_legacy_position(engine, 1, "alpaca", "MSFT") is True
    assert is_legacy_position(engine, 1, "alpaca", "NVDA") is False  # not in the snapshot -- free for V4 to consider


def test_symbol_matching_is_case_insensitive(engine):
    activate_v4(engine, 1, "alpaca", ["aapl"], set_by="test")
    assert is_legacy_position(engine, 1, "alpaca", "AAPL") is True
    assert is_legacy_position(engine, 1, "alpaca", "aapl") is True


def test_activation_is_scoped_per_user_and_broker(engine):
    activate_v4(engine, 1, "alpaca", ["AAPL"], set_by="test")
    # A different user, same broker -- not activated, must still be
    # fully conservative (everything legacy), not silently inherit
    # user 1's activation state.
    assert is_legacy_position(engine, 2, "alpaca", "AAPL") is True
    state_user2 = get_activation_state(engine, 2, "alpaca")
    assert state_user2.activated is False


def test_double_activation_is_refused_not_silently_overwritten():
    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    activate_v4(engine, 1, "alpaca", ["AAPL"], set_by="first")
    with pytest.raises(RuntimeError, match="already activated"):
        activate_v4(engine, 1, "alpaca", ["MSFT"], set_by="second")
    # The ORIGINAL snapshot must survive untouched -- AAPL still legacy,
    # MSFT never added.
    assert is_legacy_position(engine, 1, "alpaca", "AAPL") is True
    assert is_legacy_position(engine, 1, "alpaca", "MSFT") is False


def test_activate_v4_records_a_real_timestamp_and_set_by(engine):
    now = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
    state = activate_v4(engine, 1, "alpaca", ["AAPL"], set_by="dashboard:test@example.com", now=now)
    assert state.activated_at == now
    persisted = get_activation_state(engine, 1, "alpaca")
    assert persisted.activated_at == now


def test_once_legacy_always_legacy_even_with_an_empty_snapshot(engine):
    # A real edge case: V4 activated while the account was genuinely
    # flat (no open positions at all) -- every future position in every
    # symbol is then free for V4, which is correct (nothing pre-existed).
    activate_v4(engine, 1, "alpaca", [], set_by="test")
    assert is_legacy_position(engine, 1, "alpaca", "AAPL") is False

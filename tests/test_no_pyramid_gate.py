"""Unit tests for the no_pyramid_same_symbol gate added to
src/risk/governor.py's evaluate() and revalidate_before_submission()
(Equity V2 Phase 14).

Real bug this fixes, confirmed live 2026-10-02 against real production
order history (not hypothetical): without this gate, the system kept
adding to an already-open AAPL short (4 separate real orders within ~40
minutes, each smaller only because available margin was shrinking) and
attempted dozens more same-direction SELL orders on MSFT over several
days — the governor only ever had an AGGREGATE open-position count and a
coarse long/short-bucket total, neither of which can see "this exact
symbol already has a position." The only thing that ever stopped it was
running out of buying power (403 errors), never a deliberate risk
decision.

Uses an isolated in-memory SQLite engine (never the real production DB),
same pattern as tests/test_risk_governor_kill_switch.py.

Run: .venv/Scripts/python.exe -m pytest tests/test_no_pyramid_gate.py -v
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import create_engine

from src.data.db import metadata
from src.risk import governor


def _fresh_engine():
    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    return engine


def _gate_names(decision):
    return {g.name: g.passed for g in decision.gates}


def _evaluate(engine, *, action="SELL", existing_position_direction=None, instrument="AAPL"):
    return governor.evaluate(
        engine,
        user_id=1,
        instrument=instrument,
        action=action,
        confidence=0.9,
        stop_distance=2.0,
        current_price=180.0,
        current_spread=0.01,
        recent_median_spread=None,
        data_freshness_seconds={},
        account_balance=100_000.0,
        account_nav=100_000.0,
        open_position_count=1,
        open_positions_usd_direction={},
        component_scores={},
        calendar_covers_currency=True,
        upcoming_tier1_event_within_lockout=False,
        reconciliation_ok=True,
        now=datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc),
        existing_position_direction=existing_position_direction,
    )


def _revalidate(engine, *, action="SELL", existing_position_direction=None, instrument="AAPL"):
    return governor.revalidate_before_submission(
        engine,
        user_id=1,
        instrument=instrument,
        action=action,
        account_nav=100_000.0,
        current_price=180.0,
        open_position_count=1,
        open_positions_usd_direction={},
        open_positions_stop_risk_usd=None,
        approved_size_units=100.0,
        reconciliation_ok=True,
        now=datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc),
        existing_position_direction=existing_position_direction,
    )


# --- evaluate() ---

def test_evaluate_reproduces_the_real_bug_repeated_same_direction_sell_blocked():
    # Reproduces the exact real-world pattern: AAPL already has an open
    # SHORT position (from an earlier real SELL fill), and a new SELL
    # signal fires again -- this is exactly what kept happening live
    # before the fix. Must now be blocked.
    engine = _fresh_engine()
    decision = _evaluate(engine, action="SELL", existing_position_direction="short")
    assert decision.approved is False
    assert "no-pyramid" in decision.reason
    assert _gate_names(decision)["no_pyramid_same_symbol"] is False


def test_evaluate_repeated_same_direction_buy_blocked():
    engine = _fresh_engine()
    decision = _evaluate(engine, action="BUY", existing_position_direction="long")
    assert decision.approved is False
    assert _gate_names(decision)["no_pyramid_same_symbol"] is False


def test_evaluate_opposite_direction_signal_is_not_blocked_by_this_gate():
    # The model now wants to reduce/close/reverse an existing short --
    # a legitimate, different case this gate was never evidence of a
    # problem with. The gate itself must pass (other gates may still
    # independently reject for unrelated reasons, not tested here).
    engine = _fresh_engine()
    decision = _evaluate(engine, action="BUY", existing_position_direction="short")
    assert _gate_names(decision)["no_pyramid_same_symbol"] is True


def test_evaluate_no_existing_position_passes_trivially():
    engine = _fresh_engine()
    decision = _evaluate(engine, action="SELL", existing_position_direction=None)
    assert _gate_names(decision)["no_pyramid_same_symbol"] is True


def test_evaluate_gate_runs_before_sizing_so_rejection_is_clean():
    engine = _fresh_engine()
    decision = _evaluate(engine, action="SELL", existing_position_direction="short")
    assert decision.size_units is None


# --- revalidate_before_submission() ---

def test_revalidate_reproduces_the_real_bug_blocked():
    engine = _fresh_engine()
    decision = _revalidate(engine, action="SELL", existing_position_direction="short")
    assert decision.approved is False
    assert _gate_names(decision)["no_pyramid_same_symbol"] is False


def test_revalidate_opposite_direction_not_blocked_by_this_gate():
    engine = _fresh_engine()
    decision = _revalidate(engine, action="BUY", existing_position_direction="short")
    assert _gate_names(decision)["no_pyramid_same_symbol"] is True


def test_revalidate_no_existing_position_passes_trivially():
    engine = _fresh_engine()
    decision = _revalidate(engine, action="SELL", existing_position_direction=None)
    assert _gate_names(decision)["no_pyramid_same_symbol"] is True


# --- compute_open_directions_by_instrument (src/run_loop.py) ---

def test_compute_open_directions_alpaca_long_and_short():
    from src.run_loop import compute_open_directions_by_instrument
    positions_raw = [
        {"symbol": "MSFT", "qty": "138", "side": "long", "avg_entry_price": "516.86"},
        {"symbol": "AAPL", "qty": "771", "side": "short", "avg_entry_price": "331.47"},
    ]
    result = compute_open_directions_by_instrument(positions_raw, "alpaca", "paper")
    assert result == {"MSFT": "long", "AAPL": "short"}


def test_compute_open_directions_omits_flat_positions():
    from src.run_loop import compute_open_directions_by_instrument
    # A position whose side/qty combination nets to exactly zero units --
    # real brokers don't normally return a flat position row at all, but
    # the helper must not record a direction for it if one ever appeared.
    positions_raw = [{"symbol": "NVDA", "qty": "0", "side": "long", "avg_entry_price": "180.0"}]
    result = compute_open_directions_by_instrument(positions_raw, "alpaca", "paper")
    assert result == {}

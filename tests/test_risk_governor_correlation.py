"""Unit tests for the risk governor's correlation/concentration gate
(MAX_CORRELATED_EXPOSURE_PCT), redefined 2026-09-22 (external review,
P0-01) to measure aggregate USD risk-at-stop instead of notional — see
src/risk/governor.py's evaluate() and src/run_loop.py's
compute_correlated_stop_risk for the full rationale.

Uses an isolated in-memory SQLite engine (never the real production DB —
this system has live --auto-execute trading, so risk-governor changes are
always verified against a throwaway engine, per this project's standing
practice).

Run: .venv/Scripts/python.exe -m pytest tests/test_risk_governor_correlation.py -v
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


def _evaluate(engine, *, open_positions_stop_risk_usd=None, account_nav=100_000.0, now=None):
    return governor.evaluate(
        engine,
        user_id=1,
        instrument="USD_JPY",
        action="BUY",
        confidence=0.9,  # well clear of MIN_CONFIDENCE
        stop_distance=0.5,
        current_price=150.0,
        current_spread=0.01,
        recent_median_spread=None,  # spread gate not evaluated
        data_freshness_seconds={},  # no stale feeds
        account_balance=account_nav,
        account_nav=account_nav,
        open_position_count=1,
        open_positions_usd_direction={},  # notional — irrelevant to this gate now
        open_positions_stop_risk_usd=open_positions_stop_risk_usd,
        component_scores={},  # no disagreement
        calendar_covers_currency=True,
        upcoming_tier1_event_within_lockout=False,
        reconciliation_ok=True,
        now=now or datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc),
    )


def _gate(decision, name: str):
    return next(g for g in decision.gates if g.name == name)


def test_correlation_gate_blocks_when_risk_at_stop_exceeds_cap():
    engine = _fresh_engine()
    # Existing long_usd risk-at-stop = $6,000 on $100,000 NAV = 6% > 5% cap.
    decision = _evaluate(engine, open_positions_stop_risk_usd={"long_usd": 6_000.0})
    assert decision.approved is False
    assert decision.reason == "correlated risk-at-stop cap reached"
    assert _gate(decision, "correlation").passed is False


def test_correlation_gate_passes_when_risk_at_stop_under_cap():
    engine = _fresh_engine()
    # Existing long_usd risk-at-stop = $3,000 on $100,000 NAV = 3% < 5% cap.
    decision = _evaluate(engine, open_positions_stop_risk_usd={"long_usd": 3_000.0})
    assert _gate(decision, "correlation").passed is True
    assert decision.approved is True
    assert decision.size_units is not None and decision.size_units > 0


def test_correlation_gate_fails_closed_on_unprotected_position():
    engine = _fresh_engine()
    # An existing long_usd position has NO live stop -> reported as inf.
    decision = _evaluate(engine, open_positions_stop_risk_usd={"long_usd": float("inf")})
    assert decision.approved is False
    assert decision.reason == "correlated risk-at-stop cap reached"


def test_correlation_gate_defaults_to_zero_risk_when_not_supplied():
    # Backward-compatible default (e.g. PaperBroker cycles, which skip
    # stop-risk computation entirely — see run_loop.py's comment).
    engine = _fresh_engine()
    decision = _evaluate(engine, open_positions_stop_risk_usd=None)
    assert _gate(decision, "correlation").passed is True
    assert decision.approved is True

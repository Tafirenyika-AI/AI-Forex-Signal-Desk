"""V4 Priority 2 (docs/V4_ARCHITECTURE.md Section 14 gap): tests for
src/evaluation/rejected_signal_report.py.

This module deliberately does NOT build a new rejected_signal_outcomes
table or scheduled job -- investigating the real gap first found that
src/scripts/evaluate_challengers.py already re-prices every action !=
NO_TRADE trade_intent at horizon expiry regardless of risk-approval status
(confirmed live: 8,853/10,778 real RISK_REJECTED signals in production
already have a scored signal_evaluations row). These tests cover the
query-layer segmentation that was actually missing.

Run: .venv/Scripts/python.exe -m pytest tests/test_rejected_signal_report.py -v
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from sqlalchemy import create_engine, insert

from src.data.db import metadata
from src.data.db import risk_decisions as risk_decisions_table
from src.data.db import signal_evaluations as signal_evaluations_table
from src.data.db import trade_intents as trade_intents_table
from src.evaluation.rejected_signal_report import rejected_signal_segmented_report

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def engine():
    eng = create_engine("sqlite:///:memory:")
    metadata.create_all(eng)
    return eng


def _seed(engine, *, intent_id, user_id, action, approved, reason, hit, move_in_favor):
    with engine.begin() as conn:
        conn.execute(insert(trade_intents_table).values(
            id=intent_id, user_id=user_id, time=NOW, instrument="AAPL", action=action,
            confidence=0.6, horizon="1h", status="RISK_REJECTED" if not approved else "AUTO_EXECUTED",
            reference_price=100.0, broker="alpaca",
        ))
        conn.execute(insert(risk_decisions_table).values(
            user_id=user_id, trade_intent_id=intent_id, time=NOW, approved=approved, reason=reason,
        ))
        conn.execute(insert(signal_evaluations_table).values(
            user_id=user_id, source="champion", trade_intent_id=intent_id, instrument="AAPL",
            horizon="1h", action=action, signal_time=NOW, reference_price=100.0,
            expiry_price=101.0 if hit else 99.0, hit=hit, move_in_favor=move_in_favor, evaluated_at=NOW,
        ))


def test_groups_by_approval_and_reason_with_real_hit_rates(engine):
    _seed(engine, intent_id=1, user_id=1, action="BUY", approved=False,
          reason="confidence below threshold", hit=True, move_in_favor=1.0)
    _seed(engine, intent_id=2, user_id=1, action="BUY", approved=False,
          reason="confidence below threshold", hit=False, move_in_favor=-1.0)
    _seed(engine, intent_id=3, user_id=1, action="SELL", approved=True,
          reason="approved", hit=True, move_in_favor=2.0)

    report = rejected_signal_segmented_report(engine)
    by_reason = {(g.approved, g.reason): g for g in report}

    rejected_group = by_reason[(False, "confidence below threshold")]
    assert rejected_group.n == 2
    assert rejected_group.hit_rate == pytest.approx(0.5)
    assert rejected_group.mean_move_in_favor == pytest.approx(0.0)

    approved_group = by_reason[(True, "approved")]
    assert approved_group.n == 1
    assert approved_group.hit_rate == pytest.approx(1.0)


def test_excludes_no_trade_decisions(engine):
    # A NO_TRADE decision never gets a champion signal_evaluations row, so
    # the join naturally excludes it -- confirming no crash and no entry
    # for a reason that never corresponds to a real rejected signal.
    with engine.begin() as conn:
        conn.execute(insert(trade_intents_table).values(
            id=1, user_id=1, time=NOW, instrument="AAPL", action="NO_TRADE",
            confidence=0.3, horizon="1h", status="RISK_REJECTED", reference_price=100.0, broker="alpaca",
        ))
        conn.execute(insert(risk_decisions_table).values(
            user_id=1, trade_intent_id=1, time=NOW, approved=False, reason="decision was NO_TRADE",
        ))

    report = rejected_signal_segmented_report(engine)
    assert report == []


def test_filters_by_user_id(engine):
    _seed(engine, intent_id=1, user_id=1, action="BUY", approved=False,
          reason="confidence below threshold", hit=True, move_in_favor=1.0)
    _seed(engine, intent_id=2, user_id=2, action="BUY", approved=False,
          reason="confidence below threshold", hit=False, move_in_favor=-1.0)

    report_user1 = rejected_signal_segmented_report(engine, user_id=1)
    assert len(report_user1) == 1
    assert report_user1[0].n == 1

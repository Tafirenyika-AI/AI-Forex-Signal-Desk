"""Unit tests for Equity V2 Phase 17: src/evaluation/decision_trace.py.

Uses an isolated in-memory SQLite engine. A separate live check (not a
pytest test) was run against a real recent trade_intent_id in production;
see docs/EQUITY_V2_IMPLEMENTATION_LOG.md.

Run: .venv/Scripts/python.exe -m pytest tests/test_decision_trace.py -v
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

import json

import pytest
from sqlalchemy import create_engine, insert

from src.data.db import metadata
from src.data.db import model_registry as model_registry_table
from src.data.db import orders_fills as orders_fills_table
from src.data.db import predictions as predictions_table
from src.data.db import risk_decisions as risk_decisions_table
from src.data.db import trade_intents as trade_intents_table
from src.data.db import trade_outcomes as trade_outcomes_table
from src.evaluation.decision_trace import build_decision_trace, explain_decision


@pytest.fixture()
def engine():
    eng = create_engine("sqlite:///:memory:")
    metadata.create_all(eng)
    return eng


def test_unknown_trade_intent_id_returns_not_found(engine):
    trace = build_decision_trace(engine, 999999)
    assert trace.found is False
    assert "No trade_intent" in explain_decision(trace)


def test_full_trace_joins_every_table(engine):
    now = datetime(2026, 10, 2, 10, 32, 17, tzinfo=timezone.utc)
    with engine.begin() as conn:
        result = conn.execute(insert(trade_intents_table).values(
            user_id=1, time=now, instrument="NVDA", action="BUY", confidence=0.72, horizon="1h",
            regime="TREND", explanation="price component strongly bullish", status="AUTO_EXECUTED",
            key_drivers_json=json.dumps(["price momentum", "sector strength"]),
            data_freshness_json=json.dumps({"price": 2.1}), reference_price=180.5,
        ))
        trade_intent_id = result.inserted_primary_key[0]

        conn.execute(insert(predictions_table).values(
            time=now, instrument="NVDA", horizon="1h", component="price", p_up=0.71, expected_move=0.01,
        ))
        conn.execute(insert(risk_decisions_table).values(
            user_id=1, trade_intent_id=trade_intent_id, time=now, approved=True, reason="all gates cleared",
            size_units=50.0, gates_json=json.dumps([{"name": "confidence", "passed": True}]),
        ))
        conn.execute(insert(orders_fills_table).values(
            user_id=1, time=now, client_order_id=f"intent-{trade_intent_id}", instrument="NVDA",
            units=50.0, status="FILLED", fill_price=180.6, execution_mode="paper", broker="alpaca",
        ))
        conn.execute(insert(trade_outcomes_table).values(
            user_id=1, trade_intent_id=trade_intent_id, broker_trade_id="x", execution_mode="paper",
            instrument="NVDA", action="BUY", units=50.0, exit_price=185.0, realized_pl_usd=220.0,
            outcome="WIN", closed_at=now, synced_at=now, broker="alpaca",
        ))
        conn.execute(insert(model_registry_table).values(
            name="meta_model", version="v7", trained_at=now, deployed=True,
        ))

    trace = build_decision_trace(engine, trade_intent_id)
    assert trace.found is True
    assert trace.instrument == "NVDA"
    assert trace.action == "BUY"
    assert trace.key_drivers == ["price momentum", "sector strength"]
    assert len(trace.component_predictions) == 1
    assert trace.component_predictions[0]["component"] == "price"
    assert trace.risk_decision["approved"] is True
    assert trace.risk_decision["gates"] == [{"name": "confidence", "passed": True}]
    assert trace.order_fill["status"] == "FILLED"
    assert trace.outcome["realized_pl_usd"] == 220.0
    assert trace.meta_model_currently_deployed_version == "v7"

    narrative = explain_decision(trace)
    assert "NVDA" in narrative
    assert "BUY" in narrative
    assert "APPROVED" in narrative
    assert "WIN" in narrative


def test_trace_with_no_risk_decision_or_order_reports_both_as_missing(engine):
    now = datetime(2026, 10, 2, 11, 0, 0, tzinfo=timezone.utc)
    with engine.begin() as conn:
        result = conn.execute(insert(trade_intents_table).values(
            user_id=1, time=now, instrument="AAPL", action="SELL", confidence=0.6, horizon="1h", status="PROPOSED",
        ))
        trade_intent_id = result.inserted_primary_key[0]

    trace = build_decision_trace(engine, trade_intent_id)
    assert trace.risk_decision is None
    assert trace.order_fill is None
    assert trace.outcome is None
    narrative = explain_decision(trace)
    assert "No risk_decisions row" in narrative


def test_risk_rejected_trace_never_claims_an_order_was_sent(engine):
    now = datetime(2026, 10, 2, 12, 0, 0, tzinfo=timezone.utc)
    with engine.begin() as conn:
        result = conn.execute(insert(trade_intents_table).values(
            user_id=1, time=now, instrument="MSFT", action="BUY", confidence=0.4, horizon="1h", status="RISK_REJECTED",
        ))
        trade_intent_id = result.inserted_primary_key[0]
        conn.execute(insert(risk_decisions_table).values(
            user_id=1, trade_intent_id=trade_intent_id, time=now, approved=False,
            reason="confidence below threshold", size_units=None, gates_json=None,
        ))

    trace = build_decision_trace(engine, trade_intent_id)
    assert trace.risk_decision["approved"] is False
    assert trace.order_fill is None
    narrative = explain_decision(trace)
    assert "REJECTED" in narrative
    assert "never actually sent" not in narrative  # that phrase only applies to the approved-but-unfilled case


def test_outcome_without_a_joinable_order_fill_is_not_claimed_never_sent(engine):
    # Real bug caught live against a real historic trade (a genuine
    # +$8,902 NVDA win whose client_order_id predates the
    # f"intent-{id}" convention this join relies on): order_fill can be
    # None while an outcome row proves a fill genuinely happened. The
    # narrative must never claim "never actually sent" in that case.
    now = datetime(2026, 9, 10, 18, 25, 49, tzinfo=timezone.utc)
    with engine.begin() as conn:
        result = conn.execute(insert(trade_intents_table).values(
            user_id=1, time=now, instrument="NVDA", action="BUY", confidence=0.42, horizon="15m", status="AUTHORIZED_EXECUTED",
        ))
        trade_intent_id = result.inserted_primary_key[0]
        conn.execute(insert(risk_decisions_table).values(
            user_id=1, trade_intent_id=trade_intent_id, time=now, approved=True,
            reason="approved", size_units=652.0, gates_json=None,
        ))
        # No orders_fills row inserted at all -- this trade's real
        # client_order_id was a raw UUID, not f"intent-{trade_intent_id}".
        conn.execute(insert(trade_outcomes_table).values(
            user_id=1, trade_intent_id=trade_intent_id, broker_trade_id="real-uuid", execution_mode="demo",
            instrument="NVDA", action="BUY", units=652.0, entry_price=217.98, exit_price=231.63,
            realized_pl_usd=8902.49, outcome="WIN", closed_at=now, synced_at=now, broker="alpaca",
        ))

    trace = build_decision_trace(engine, trade_intent_id)
    assert trace.order_fill is None
    assert trace.outcome is not None
    narrative = explain_decision(trace)
    assert "never actually sent" not in narrative
    assert "a fill clearly did happen" in narrative
    assert "WIN" in narrative


def test_no_meta_model_deployed_yet_is_honestly_none(engine):
    now = datetime(2026, 10, 2, 13, 0, 0, tzinfo=timezone.utc)
    with engine.begin() as conn:
        result = conn.execute(insert(trade_intents_table).values(
            user_id=1, time=now, instrument="NVDA", action="BUY", confidence=0.5, horizon="1h", status="PROPOSED",
        ))
        trade_intent_id = result.inserted_primary_key[0]
    trace = build_decision_trace(engine, trade_intent_id)
    assert trace.meta_model_currently_deployed_version is None

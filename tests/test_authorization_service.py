"""Unit/integration tests for P0-03 (external review, 2026-09-22):
src/authorization/service.py's authorize() — atomic claim (no duplicate
submission race), crypto fractional sizing, stable client_order_id, and
fresh portfolio-state revalidation immediately before submission.

Uses an isolated in-memory SQLite engine + fake broker objects (no real
network/broker calls) — never the real production DB or a real broker,
consistent with this project's standing practice for anything that could
place an order.

Run: .venv/Scripts/python.exe -m pytest tests/test_authorization_service.py -v
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import create_engine, insert, select

from src.authorization import service as auth_service
from src.broker.base import AccountState, OrderResult, Price
from src.data.db import metadata
from src.data.db import risk_decisions as risk_decisions_table
from src.data.db import trade_intents as trade_intents_table
from src.execution.service import ExecutionService
from src.risk import governor


def _fresh_engine():
    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    return engine


def _seed_intent(engine, *, instrument="USD_JPY", size_units=100_000.0, now=None):
    now = now or datetime.now(timezone.utc)
    with engine.begin() as conn:
        intent_id = conn.execute(
            insert(trade_intents_table).values(
                user_id=1, time=now, instrument=instrument, action="BUY", confidence=0.9,
                horizon="1h", stop_distance=0.5, take_profit_distance=1.0,
                data_freshness_json=json.dumps({}), status="AWAITING_AUTHORIZATION",
                reference_price=150.0, broker="oanda" if "/" not in instrument else "alpaca",
            )
        ).inserted_primary_key[0]
        conn.execute(
            insert(risk_decisions_table).values(
                user_id=1, trade_intent_id=intent_id, time=now, approved=True, reason="approved",
                size_units=size_units,
            )
        )
    return intent_id


class _FakeBroker:
    """Minimal broker covering everything authorize() touches: account
    state, positions (empty -- no existing exposure), OANDA open_trades()
    (for compute_correlated_stop_risk), current price, and order
    placement. place_order always fills at the requested price."""

    def __init__(self, nav=100_000.0):
        self.nav = nav
        self.place_order_calls = []

    async def account_state(self):
        return AccountState(
            account_id="fake", currency="USD", balance=self.nav, nav=self.nav,
            unrealized_pl=0.0, margin_used=0.0, margin_available=self.nav,
            open_trade_count=0, open_position_count=0,
        )

    async def positions(self):
        return []

    async def open_trades(self):
        return []

    async def list_instruments(self):
        return ["USD_JPY"]

    async def get_current_prices(self, instruments):
        return [Price(instrument=i, time=datetime.now(timezone.utc), bid=149.999, ask=150.001) for i in instruments]

    async def place_order(self, *, instrument, units, client_order_id, stop_loss_price=None,
                           take_profit_price=None, order_type="MARKET", limit_price=None):
        self.place_order_calls.append(client_order_id)
        return OrderResult(
            client_order_id=client_order_id, broker_order_id=f"BROKER-{client_order_id}",
            broker_transaction_id=f"TXN-{client_order_id}", status="FILLED",
            raw={"fill_price": 150.0, "spread": 0.002},
        )


def _run(coro):
    return asyncio.run(coro)


def test_manual_approval_places_exactly_one_order_and_marks_executed():
    engine = _fresh_engine()
    intent_id = _seed_intent(engine)
    broker = _FakeBroker()
    execution_service = ExecutionService(broker, engine, execution_mode="demo", user_id=1)

    result = _run(auth_service.authorize(engine, broker, execution_service, intent_id, "APPROVED"))
    assert result.decision == "APPROVED"
    assert result.order_result.status == "FILLED"
    assert len(broker.place_order_calls) == 1

    with engine.connect() as conn:
        row = conn.execute(select(trade_intents_table).where(trade_intents_table.c.id == intent_id)).mappings().first()
    assert row["status"] == "EXECUTED"


def test_concurrent_authorize_calls_place_exactly_one_order():
    # T10 from the external review brief: two concurrent calls on the same
    # pending intent must result in ONE broker submission, not two.
    engine = _fresh_engine()
    intent_id = _seed_intent(engine)
    broker = _FakeBroker()
    execution_service = ExecutionService(broker, engine, execution_mode="demo", user_id=1)

    async def _race():
        return await asyncio.gather(
            auth_service.authorize(engine, broker, execution_service, intent_id, "APPROVED"),
            auth_service.authorize(engine, broker, execution_service, intent_id, "APPROVED"),
        )

    results = _run(_race())
    decisions = sorted(r.decision for r in results)
    assert decisions == ["APPROVED", "SKIPPED"]
    assert len(broker.place_order_calls) == 1
    # Both calls used the SAME stable client_order_id -- the second layer
    # of protection even if the atomic claim had somehow not been enough.
    assert broker.place_order_calls[0] == f"intent-{intent_id}"


def test_crypto_fractional_size_not_truncated():
    engine = _fresh_engine()
    intent_id = _seed_intent(engine, instrument="BTC/USD", size_units=0.25)
    broker = _FakeBroker()
    execution_service = ExecutionService(broker, engine, execution_mode="demo", user_id=1)

    captured = {}
    orig_execute = execution_service.execute

    async def _capture_execute(**kwargs):
        captured.update(kwargs)
        return await orig_execute(**kwargs)

    execution_service.execute = _capture_execute
    result = _run(auth_service.authorize(engine, broker, execution_service, intent_id, "APPROVED"))
    assert result.decision == "APPROVED"
    assert captured["size_units"] == 0.25  # NOT int(0.25) == 0


def test_manual_kill_switch_blocks_submission_even_with_stale_approved_risk():
    # The stored risk_decision was approved before the kill switch tripped
    # -- authorize() must re-check CURRENT state, not just trust the old row.
    engine = _fresh_engine()
    intent_id = _seed_intent(engine)
    governor.set_manual_kill_switch(engine, 1, "oanda", True, "tripped after signal was approved",
                                     "dashboard:test", datetime.now(timezone.utc))
    broker = _FakeBroker()
    execution_service = ExecutionService(broker, engine, execution_mode="demo", user_id=1)

    result = _run(auth_service.authorize(engine, broker, execution_service, intent_id, "APPROVED"))
    assert result.decision == "APPROVED"
    assert result.order_result is None
    assert "no longer clears" in result.detail
    assert len(broker.place_order_calls) == 0

    with engine.connect() as conn:
        row = conn.execute(select(trade_intents_table).where(trade_intents_table.c.id == intent_id)).mappings().first()
    # Reverted back to AWAITING_AUTHORIZATION, not stuck -- retryable once cleared.
    assert row["status"] == "AWAITING_AUTHORIZATION"


def test_paper_broker_authorize_does_not_crash_building_fresh_risk_check():
    # Real bug caught before it shipped: PaperBroker has no list_instruments()
    # (it only ever simulates OANDA trades against its own DB ledger, never
    # a real broker's instrument catalog) -- the new fresh-portfolio-state
    # revalidation this test file covers above would have raised
    # AttributeError the first time anyone approved a paper-mode trade,
    # since _build_usd_conversion_rates() unconditionally calls it for any
    # "oanda"-kind instrument. Uses a REAL PaperBroker (not a fake) against
    # this test's in-memory engine -- account_state()/positions()/
    # place_order() are all DB-only, no network; only get_current_prices()
    # (which delegates to a real OandaBroker for genuine market data) is
    # monkeypatched here to avoid a real network call in a unit test.
    from src.config import Settings
    from src.execution.paper_broker import PaperBroker

    engine = _fresh_engine()
    intent_id = _seed_intent(engine)
    settings = Settings(
        oanda_api_token="fake", oanda_environment="practice", oanda_account_id="fake",
        db_path=Path(":memory:"), fred_api_key=None, alphavantage_api_key=None,
    )
    broker = PaperBroker(settings, engine, user_id=1)
    broker.get_current_prices = lambda instruments: asyncio.sleep(0, result=[
        Price(instrument=i, time=datetime.now(timezone.utc), bid=149.999, ask=150.001) for i in instruments
    ])
    execution_service = ExecutionService(broker, engine, execution_mode="paper", user_id=1)

    result = _run(auth_service.authorize(engine, broker, execution_service, intent_id, "APPROVED"))
    assert result.decision == "APPROVED"  # did not raise AttributeError


def test_reject_does_not_touch_broker():
    engine = _fresh_engine()
    intent_id = _seed_intent(engine)
    broker = _FakeBroker()
    execution_service = ExecutionService(broker, engine, execution_mode="demo", user_id=1)

    result = _run(auth_service.authorize(engine, broker, execution_service, intent_id, "REJECTED"))
    assert result.decision == "REJECTED"
    assert len(broker.place_order_calls) == 0
    with engine.connect() as conn:
        row = conn.execute(select(trade_intents_table).where(trade_intents_table.c.id == intent_id)).mappings().first()
    assert row["status"] == "REJECTED_BY_USER"

"""Equity V2 Phase 19 — dedicated coverage for two safety properties the
brief names explicitly but which had no direct test of their own before
this: ORDER IDEMPOTENCY (src/execution/service.py's own
on_conflict_do_nothing(index_elements=["client_order_id"]) mechanism) and
RESTART RECOVERY (a process restart mid-cycle, or a second scheduled run
before the first's effects are visible, must never duplicate an order
for a position that already exists).

Order idempotency previously only had INDIRECT coverage, via
tests/test_authorization_service.py's own concurrency test
(test_concurrent_authorize_calls_place_exactly_one_order) — real and
valuable, but exercised through the higher-level authorization flow, not
src/execution/service.py's own idempotency mechanism directly. Restart
recovery's main real guarantee is Phase 14's no_pyramid_same_symbol gate
(already tested in tests/test_no_pyramid_gate.py) — this file adds one
test framing that exact scenario explicitly as "a restart/re-run must not
duplicate a position," for direct traceability to this brief's own named
testing dimension, rather than relying on it being implicit in a
differently-titled test file.

Run: .venv/Scripts/python.exe -m pytest tests/test_execution_idempotency_and_restart.py -v
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import create_engine, func, select

from src.broker.base import OrderResult
from src.data.db import metadata
from src.data.db import orders_fills as orders_fills_table
from src.execution.service import ExecutionService
from src.risk import governor


def _fresh_engine():
    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    return engine


class _FakeBroker:
    """Always succeeds -- the test only cares about ExecutionService's own
    DB-write idempotency, not broker-side behavior."""

    def __init__(self):
        self.place_order_calls = 0

    async def place_order(self, *, instrument, units, client_order_id, stop_loss_price, take_profit_price, order_type):
        self.place_order_calls += 1
        return OrderResult(
            client_order_id=client_order_id, broker_order_id=f"broker-{self.place_order_calls}",
            broker_transaction_id=None, status="FILLED", raw={"fill_price": 100.0},
        )


def _run(coro):
    import asyncio
    return asyncio.run(coro)


# --- order idempotency (src/execution/service.py's own ON CONFLICT DO NOTHING) ---

def test_executing_the_same_client_order_id_twice_persists_exactly_one_row():
    engine = _fresh_engine()
    broker = _FakeBroker()
    service = ExecutionService(broker, engine, execution_mode="paper", user_id=1)

    async def scenario():
        await service.execute("NVDA", "BUY", 10, stop_loss_price=170.0, take_profit_price=190.0,
                               client_order_id="intent-1")
        # Retried exactly as a real retry would -- the SAME client_order_id,
        # simulating a process restart replaying a trade_intent it isn't
        # sure was already submitted.
        await service.execute("NVDA", "BUY", 10, stop_loss_price=170.0, take_profit_price=190.0,
                               client_order_id="intent-1")

    _run(scenario())

    with engine.connect() as conn:
        count = conn.execute(
            select(func.count()).select_from(orders_fills_table)
            .where(orders_fills_table.c.client_order_id == "intent-1")
        ).scalar()
    assert count == 1
    # The broker itself WAS called twice (ExecutionService doesn't dedupe
    # the broker call -- the broker's own idempotency key, client_order_id,
    # is what protects against a real duplicate fill; this project's own
    # layer only guarantees the LOG never shows a phantom duplicate row).
    assert broker.place_order_calls == 2


def test_a_broker_error_still_logs_exactly_one_row_fail_closed():
    engine = _fresh_engine()

    class _FailingBroker:
        async def place_order(self, **kwargs):
            raise RuntimeError("simulated broker outage")

    service = ExecutionService(_FailingBroker(), engine, execution_mode="paper", user_id=1)
    result = _run(service.execute("NVDA", "BUY", 10, stop_loss_price=170.0, take_profit_price=190.0,
                                    client_order_id="intent-2"))
    assert result.status.startswith("ERROR:")
    with engine.connect() as conn:
        row = conn.execute(
            select(orders_fills_table).where(orders_fills_table.c.client_order_id == "intent-2")
        ).mappings().first()
    assert row is not None
    assert row["status"].startswith("ERROR:")


# --- restart recovery (explicit framing of Phase 14's own no-pyramid gate) ---

def test_restart_or_rerun_does_not_duplicate_an_already_open_position():
    """Simulates the real scenario Critical Rule 0 and this brief's own
    'restart recovery' testing dimension both name: a process restarts (or
    a scheduled cycle re-runs) and re-evaluates an instrument that ALREADY
    has a real open position from before the restart. The risk governor
    must reject a same-direction re-entry -- this is the literal
    mechanism that makes "restart with open positions" safe, not an
    accident of some other gate."""
    engine = _fresh_engine()
    # Simulates what a restarted process would learn from a fresh
    # broker.positions() call: NVDA is already long from before the
    # restart.
    decision = governor.evaluate(
        engine, user_id=1, instrument="NVDA", action="BUY", confidence=0.9, stop_distance=2.0,
        current_price=180.0, current_spread=0.01, recent_median_spread=None, data_freshness_seconds={},
        account_balance=100_000.0, account_nav=100_000.0, open_position_count=1,
        open_positions_usd_direction={}, component_scores={}, calendar_covers_currency=True,
        upcoming_tier1_event_within_lockout=False, reconciliation_ok=True,
        now=datetime(2026, 10, 2, tzinfo=timezone.utc),
        existing_position_direction="long",  # what a post-restart broker.positions() call would show
    )
    assert decision.approved is False
    assert "no-pyramid" in decision.reason


def test_restart_recovery_a_genuinely_flat_instrument_is_unaffected():
    # The same restart scenario, but for an instrument the restarted
    # process has NO existing position in -- must evaluate normally, not
    # be over-conservatively blocked just because SOME position exists
    # elsewhere in the account.
    engine = _fresh_engine()
    decision = governor.evaluate(
        engine, user_id=1, instrument="MSFT", action="BUY", confidence=0.9, stop_distance=2.0,
        current_price=500.0, current_spread=0.01, recent_median_spread=None, data_freshness_seconds={},
        account_balance=100_000.0, account_nav=100_000.0, open_position_count=1,
        open_positions_usd_direction={}, component_scores={}, calendar_covers_currency=True,
        upcoming_tier1_event_within_lockout=False, reconciliation_ok=True,
        now=datetime(2026, 10, 2, tzinfo=timezone.utc),
        existing_position_direction=None,  # MSFT itself is flat, even though NVDA (not this call) is open
    )
    pyramid_gate = next(g for g in decision.gates if g.name == "no_pyramid_same_symbol")
    assert pyramid_gate.passed is True

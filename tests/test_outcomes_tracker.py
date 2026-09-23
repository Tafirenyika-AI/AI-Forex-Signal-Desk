"""Unit tests for P1-01 (external review, 2026-09-22): src/outcomes/
tracker.py's sync_outcomes -- per-leg closed quantity (T11), not the full
original opening size, on every trade_outcomes row.

Uses a fake OandaBroker (real OANDA transaction-ledger shape, no network)
and an isolated in-memory SQLite engine.

Run: .venv/Scripts/python.exe -m pytest tests/test_outcomes_tracker.py -v
"""
import asyncio
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import create_engine, select

from src.data.db import metadata
from src.data.db import trade_outcomes as trade_outcomes_table
from src.outcomes.tracker import sync_outcomes


def _fresh_engine():
    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    return engine


class _FakeOandaBroker:
    def __init__(self, transactions):
        self._transactions = transactions

    async def transactions(self):
        return self._transactions


def _run(coro):
    return asyncio.run(coro)


def test_t11_partial_then_final_close_report_correct_per_leg_units():
    # T11: 1,000 units opened; 400 reduced at -$5; remaining 600 closed at
    # +$10 -- required: BOTH legs recorded, each with its OWN closed
    # quantity (400 and 600), not 1,000 units on both rows.
    txns = [
        {
            "id": "1", "type": "ORDER_FILL", "instrument": "USD_JPY",
            "units": "1000", "price": "150.000", "time": "2026-09-22T10:00:00.000000000Z",
            "clientOrderID": "entry-1",
        },
        {
            "id": "2", "type": "ORDER_FILL", "instrument": "USD_JPY",
            "price": "150.500", "time": "2026-09-22T11:00:00.000000000Z",
            "tradesClosed": [{"tradeID": "1", "units": "-400", "realizedPL": "-5.0"}],
        },
        {
            "id": "3", "type": "ORDER_FILL", "instrument": "USD_JPY",
            "price": "151.200", "time": "2026-09-22T12:00:00.000000000Z",
            "tradesClosed": [{"tradeID": "1", "units": "-600", "realizedPL": "10.0"}],
        },
    ]
    engine = _fresh_engine()
    broker = _FakeOandaBroker(txns)

    new_count = _run(sync_outcomes(engine, broker, user_id=1, execution_mode="demo"))
    assert new_count == 2

    with engine.connect() as conn:
        rows = conn.execute(
            select(trade_outcomes_table).order_by(trade_outcomes_table.c.closed_at)
        ).mappings().all()

    assert len(rows) == 2
    partial, final = rows
    assert partial["units"] == 400.0
    assert partial["realized_pl_usd"] == -5.0
    assert partial["outcome"] == "LOSS"
    assert final["units"] == 600.0  # NOT 1000 -- the bug this fixes
    assert final["realized_pl_usd"] == 10.0
    assert final["outcome"] == "WIN"
    # Both legs correctly link back to the same original opening trade/entry.
    assert partial["entry_price"] == final["entry_price"] == 150.0
    assert partial["action"] == final["action"] == "BUY"


def test_single_full_close_reports_full_units_unchanged():
    # Sanity check: the common case (no partial close) must still work --
    # closed["units"] equals the full opening size here, so this also
    # confirms the fix doesn't regress the simple path.
    txns = [
        {
            "id": "1", "type": "ORDER_FILL", "instrument": "EUR_USD",
            "units": "50000", "price": "1.08000", "time": "2026-09-22T09:00:00.000000000Z",
            "clientOrderID": "entry-2",
        },
        {
            "id": "2", "type": "ORDER_FILL", "instrument": "EUR_USD",
            "price": "1.08500", "time": "2026-09-22T09:30:00.000000000Z",
            "tradesClosed": [{"tradeID": "1", "units": "-50000", "realizedPL": "25.0"}],
        },
    ]
    engine = _fresh_engine()
    broker = _FakeOandaBroker(txns)

    _run(sync_outcomes(engine, broker, user_id=1, execution_mode="demo"))

    with engine.connect() as conn:
        row = conn.execute(select(trade_outcomes_table)).mappings().first()
    assert row["units"] == 50000.0
    assert row["realized_pl_usd"] == 25.0

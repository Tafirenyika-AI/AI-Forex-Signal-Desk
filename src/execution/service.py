"""Execution Service (blueprint sec. 3 layer 9, sec. 10).

Deliberately "dumb" per sec. 10: it accepts only a broker, an already-risk-
approved size, and a stop/target — it does not interpret news, does not
recompute confidence, and cannot override the risk governor's decision.
Fail-closed per sec. 10 and sec. 15: any broker/API error returns a
rejected OrderResult rather than raising into a state where the caller
might assume a fill happened.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy.engine import Engine

from src.broker.base import OrderResult
from src.broker.registry import broker_kind_for
from src.data.db import orders_fills as orders_fills_table
from src.data.db import upsert_insert as insert

logger = logging.getLogger(__name__)


def new_client_order_id(instrument: str, action: str) -> str:
    return f"{instrument}-{action}-{uuid.uuid4().hex[:12]}"


class ExecutionService:
    def __init__(self, broker: Any, engine: Engine, execution_mode: Literal["paper", "demo"], user_id: int):
        self._broker = broker
        self._engine = engine
        self._execution_mode = execution_mode
        self._user_id = user_id

    async def execute(
        self,
        instrument: str,
        action: str,
        size_units: int | float,
        stop_loss_price: float | None,
        take_profit_price: float | None,
        client_order_id: str | None = None,
    ) -> OrderResult:
        if action not in ("BUY", "SELL"):
            raise ValueError(f"execute() called with non-tradeable action: {action}")

        client_order_id = client_order_id or new_client_order_id(instrument, action)
        signed_units = size_units if action == "BUY" else -size_units

        start = time.monotonic()
        try:
            result = await self._broker.place_order(
                instrument=instrument,
                units=signed_units,
                client_order_id=client_order_id,
                stop_loss_price=stop_loss_price,
                take_profit_price=take_profit_price,
                order_type="MARKET",
            )
        except Exception as exc:  # noqa: BLE001 - fail closed, never assume a fill happened
            logger.exception("Order placement failed for %s", client_order_id)
            result = OrderResult(
                client_order_id=client_order_id,
                broker_order_id=None,
                broker_transaction_id=None,
                status=f"ERROR:{exc!r}",
                raw={},
            )

        latency_ms = (time.monotonic() - start) * 1000
        fill_price = result.raw.get("fill_price") if isinstance(result.raw, dict) else None
        spread = result.raw.get("spread") if isinstance(result.raw, dict) else None

        # ON CONFLICT DO NOTHING, not a plain insert: a duplicate retry
        # intentionally reuses client_order_id (that's the idempotency
        # contract), so the first log entry wins and the retry is a no-op
        # here too, matching the broker-level DUPLICATE_IGNORED status.
        with self._engine.begin() as conn:
            stmt = insert(orders_fills_table).values(
                user_id=self._user_id,
                time=datetime.now(timezone.utc),
                client_order_id=client_order_id,
                broker_order_id=result.broker_order_id,
                broker_transaction_id=result.broker_transaction_id,
                instrument=instrument,
                units=signed_units,
                status=result.status,
                fill_price=fill_price,
                spread=spread,
                latency_ms=latency_ms,
                execution_mode=self._execution_mode,
                raw_json=str(result.raw)[:4000],
                broker=broker_kind_for(instrument),
            )
            stmt = stmt.on_conflict_do_nothing(index_elements=["client_order_id"])
            conn.execute(stmt)

        return result

    async def reconcile(self) -> bool:
        """Minimal Phase 3/4 reconciliation: confirm the broker connection is
        healthy and returns readable position/account state. Since every
        order flows through this service's execute(), there is no separate
        internal ledger to diff against yet — that becomes necessary once
        orders can also originate outside this service (e.g. manual
        intervention), which is out of scope for v1."""
        try:
            await self._broker.positions()
            await self._broker.account_state()
            return True
        except Exception:
            logger.exception("Reconciliation check failed")
            return False

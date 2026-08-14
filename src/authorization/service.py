"""Human authorization layer.

This is the one gate that isn't in the blueprint's own architecture table
(sec. 3) but sits logically right after the Risk Governor and before the
Execution Service: a trade that has cleared every automated gate still does
not get sent anywhere until a person explicitly approves it here. Both the
approval and the rejection are logged to `authorizations` — the audit trail
answers "who authorized this, and when" for every order this system has
ever placed, not just the ones a human said yes to.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Engine

from src.broker.base import OrderResult
from src.data.db import authorizations as authorizations_table
from src.data.db import predictions as predictions_table
from src.data.db import risk_decisions as risk_decisions_table
from src.data.db import trade_intents as trade_intents_table
from src.risk.governor import PRICE_DRIFT_STOP_RATIO
from src.execution.service import ExecutionService

# A signal is only trustworthy for roughly as long as its own stated
# horizon — the regime/price/macro/news snapshot it was built from goes
# stale after that. Sitting unauthorized longer than this auto-expires it
# rather than letting a human approve a many-hours-old read of the market.
HORIZON_TO_SECONDS = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
DEFAULT_EXPIRY_SECONDS = 14400


def _naive_to_utc(value: Any) -> Any:
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _row_to_dict(row: Any) -> dict:
    return {k: _naive_to_utc(v) for k, v in dict(row).items()}


def expire_stale_intents(engine: Engine, user_id: int, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    with engine.begin() as conn:
        pending = conn.execute(
            select(trade_intents_table).where(
                trade_intents_table.c.status == "AWAITING_AUTHORIZATION",
                trade_intents_table.c.user_id == user_id,
            )
        ).mappings().all()
        expired_ids = []
        for row in pending:
            row = _row_to_dict(row)
            max_age = HORIZON_TO_SECONDS.get(row["horizon"], DEFAULT_EXPIRY_SECONDS)
            age = (now - row["time"]).total_seconds()
            if age > max_age:
                expired_ids.append(row["id"])
        for intent_id in expired_ids:
            conn.execute(
                update(trade_intents_table)
                .where(trade_intents_table.c.id == intent_id)
                .values(status="EXPIRED")
            )
    return len(expired_ids)


def list_pending(engine: Engine, user_id: int, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    with engine.connect() as conn:
        intents = conn.execute(
            select(trade_intents_table)
            .where(
                trade_intents_table.c.status == "AWAITING_AUTHORIZATION",
                trade_intents_table.c.user_id == user_id,
            )
            .order_by(trade_intents_table.c.time.desc())
        ).mappings().all()

        results = []
        for intent in intents:
            intent = _row_to_dict(intent)
            risk = conn.execute(
                select(risk_decisions_table)
                .where(risk_decisions_table.c.trade_intent_id == intent["id"])
                .order_by(risk_decisions_table.c.id.desc())
            ).mappings().first()
            preds = conn.execute(
                select(predictions_table)
                .where(predictions_table.c.instrument == intent["instrument"])
                .where(predictions_table.c.horizon == intent["horizon"])
                .where(predictions_table.c.time == intent["time"].replace(tzinfo=None))
            ).mappings().all()

            max_age = HORIZON_TO_SECONDS.get(intent["horizon"], DEFAULT_EXPIRY_SECONDS)
            age_seconds = (now - intent["time"]).total_seconds()
            expires_at = intent["time"] + timedelta(seconds=max_age)
            time_remaining_seconds = (expires_at - now).total_seconds()

            # A signal generated in the morning can still be perfectly valid
            # at night if it's a long enough horizon, and a short-horizon one
            # can be dead in 20 minutes — the only thing worth showing a
            # human is the actual clock time it expires and how urgent that
            # is, never a raw "hours old" figure that requires mental math.
            if time_remaining_seconds <= 0:
                urgency = "expired"
            elif time_remaining_seconds < max_age * 0.2:
                urgency = "critical"
            elif time_remaining_seconds < max_age * 0.5:
                urgency = "warning"
            else:
                urgency = "fresh"

            results.append(
                {
                    **intent,
                    "key_drivers": json.loads(intent["key_drivers_json"] or "[]"),
                    "contrary_evidence": json.loads(intent["contrary_evidence_json"] or "[]"),
                    "data_freshness": json.loads(intent["data_freshness_json"] or "{}"),
                    "risk": dict(risk) if risk else None,
                    "components": [dict(p) for p in preds],
                    "age_seconds": age_seconds,
                    "age_fraction_of_horizon": age_seconds / max_age,
                    "expires_at": expires_at,
                    "time_remaining_seconds": time_remaining_seconds,
                    "urgency": urgency,
                }
            )
        return results


def list_history(engine: Engine, user_id: int, limit: int = 50) -> list[dict]:
    with engine.connect() as conn:
        auths = conn.execute(
            select(authorizations_table)
            .where(authorizations_table.c.user_id == user_id)
            .order_by(authorizations_table.c.id.desc()).limit(limit)
        ).mappings().all()

        results = []
        for auth in auths:
            auth = _row_to_dict(dict(auth))
            intent = conn.execute(
                select(trade_intents_table).where(trade_intents_table.c.id == auth["trade_intent_id"])
            ).mappings().first()
            results.append({**auth, "intent": _row_to_dict(dict(intent)) if intent else None})
        return results


def build_manual_order_ticket(intent: dict, current_price: float | None = None) -> dict:
    """Everything needed to place the equivalent order by hand on OANDA's
    own web/mobile platform, for anyone who doesn't want this system
    holding the execution credentials at all."""
    ref_price = current_price if current_price is not None else intent.get("reference_price")
    stop_distance = intent.get("stop_distance")
    tp_distance = intent.get("take_profit_distance")
    size_units = intent["risk"]["size_units"] if intent.get("risk") else None

    if ref_price is None or stop_distance is None:
        stop_price = target_price = None
    elif intent["action"] == "BUY":
        stop_price = ref_price - stop_distance
        target_price = ref_price + tp_distance if tp_distance else None
    else:
        stop_price = ref_price + stop_distance
        target_price = ref_price - tp_distance if tp_distance else None

    return {
        "instrument": intent["instrument"],
        "direction": "Buy" if intent["action"] == "BUY" else "Sell",
        "order_type": "Market",
        "units": size_units,
        "reference_price": ref_price,
        "stop_loss": round(stop_price, 5) if stop_price else None,
        "take_profit": round(target_price, 5) if target_price else None,
    }


@dataclass
class AuthorizationResult:
    decision: str
    order_result: OrderResult | None
    detail: str


async def authorize(
    engine: Engine,
    broker: Any,
    execution_service: ExecutionService,
    trade_intent_id: int,
    decision: Literal["APPROVED", "REJECTED"],
    authorized_by: str = "user",
    notes: str | None = None,
    now: datetime | None = None,
) -> AuthorizationResult:
    now = now or datetime.now(timezone.utc)

    with engine.connect() as conn:
        intent = conn.execute(
            select(trade_intents_table).where(trade_intents_table.c.id == trade_intent_id)
        ).mappings().first()
    if intent is None:
        raise ValueError(f"No trade_intent with id={trade_intent_id}")
    intent = _row_to_dict(dict(intent))

    if intent["status"] != "AWAITING_AUTHORIZATION":
        return AuthorizationResult(
            decision="SKIPPED",
            order_result=None,
            detail=f"intent status is '{intent['status']}', not AWAITING_AUTHORIZATION — already handled",
        )

    if decision == "REJECTED":
        with engine.begin() as conn:
            conn.execute(
                update(trade_intents_table)
                .where(trade_intents_table.c.id == trade_intent_id)
                .values(status="REJECTED_BY_USER")
            )
            conn.execute(
                insert(authorizations_table),
                {
                    "user_id": intent["user_id"],
                    "trade_intent_id": trade_intent_id,
                    "decision": "REJECTED",
                    "authorized_by": authorized_by,
                    "authorized_at": now,
                    "notes": notes,
                    "resulting_client_order_id": None,
                },
            )
        return AuthorizationResult("REJECTED", None, "Rejected by user; no order sent.")

    with engine.connect() as conn:
        risk = conn.execute(
            select(risk_decisions_table)
            .where(risk_decisions_table.c.trade_intent_id == trade_intent_id)
            .order_by(risk_decisions_table.c.id.desc())
        ).mappings().first()
    if risk is None or not risk["approved"] or not risk["size_units"]:
        raise ValueError(f"trade_intent {trade_intent_id} has no approved, sized risk_decision")
    size_units = int(risk["size_units"])

    prices = await broker.get_current_prices([intent["instrument"]])
    if not prices:
        return AuthorizationResult(
            "APPROVED", None,
            "Could not fetch a current price — order NOT sent. Try again.",
        )
    current_price = prices[0].mid

    # Price-drift / slippage guard (Autonomous Upgrade Spec sec. 17). This
    # is the one gap risk_governor.evaluate()'s own drift gate can't reach:
    # that gate runs once, in the same cycle the signal was generated, when
    # current_price == reference_price by construction — drift only
    # actually accumulates during the human review gap between signal and
    # this authorize() call, so it has to be checked here, right before the
    # stale size/stop/target actually get sent.
    stop_distance = intent["stop_distance"]
    reference_price = intent.get("reference_price")
    if reference_price is not None and stop_distance:
        drift = abs(current_price - reference_price)
        if drift > PRICE_DRIFT_STOP_RATIO * stop_distance:
            return AuthorizationResult(
                "APPROVED", None,
                f"Price drifted {drift:.5f} since the signal was generated "
                f"(vs {PRICE_DRIFT_STOP_RATIO:.0%} of the {stop_distance:.5f} stop distance) — "
                "order NOT sent. The setup this signal reasoned about may no longer hold; "
                "wait for a fresh signal next cycle.",
            )

    tp_distance = intent["take_profit_distance"]
    if intent["action"] == "BUY":
        stop_price = current_price - stop_distance if stop_distance else None
        target_price = current_price + tp_distance if tp_distance else None
    else:
        stop_price = current_price + stop_distance if stop_distance else None
        target_price = current_price - tp_distance if tp_distance else None

    result = await execution_service.execute(
        instrument=intent["instrument"],
        action=intent["action"],
        size_units=size_units,
        stop_loss_price=stop_price,
        take_profit_price=target_price,
    )

    new_status = "EXECUTED" if result.status == "FILLED" else "EXECUTION_FAILED"
    with engine.begin() as conn:
        conn.execute(
            update(trade_intents_table)
            .where(trade_intents_table.c.id == trade_intent_id)
            .values(status=new_status)
        )
        conn.execute(
            insert(authorizations_table),
            {
                "user_id": intent["user_id"],
                "trade_intent_id": trade_intent_id,
                "decision": "APPROVED",
                "authorized_by": authorized_by,
                "authorized_at": now,
                "notes": notes,
                "resulting_client_order_id": result.client_order_id,
            },
        )

    return AuthorizationResult("APPROVED", result, f"Order status: {result.status}")

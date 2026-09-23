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
from src.broker.registry import broker_kind_for
from src.data.db import authorizations as authorizations_table
from src.data.db import predictions as predictions_table
from src.data.db import risk_decisions as risk_decisions_table
from src.data.db import trade_intents as trade_intents_table
from src.risk.governor import PRICE_DRIFT_STOP_RATIO
from src.risk import governor as risk_governor
from src.execution.paper_broker import PaperBroker
from src.execution.service import ExecutionService
from src.run_loop import _build_usd_conversion_rates, compute_correlated_stop_risk, compute_exposure

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
    calling_user_id: int,
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

    # Real bug found 2026-09-22 (external review, P0-05): this had no
    # ownership check at all — trade_intent_id is a sequential, trivially
    # guessable integer primary key, and this function's ONLY protection
    # against approving/rejecting a DIFFERENT user's pending trade was
    # relying entirely on the dashboard's own list_pending() UI never
    # showing another user's id, with no server-side backstop. Worse than
    # a read: the broker/execution_service passed in belong to whoever is
    # CURRENTLY LOGGED IN, not the intent's actual owner — so a mismatch
    # would place a real order using the WRONG user's broker credentials,
    # sized/directed by another user's private signal data. Enforced here,
    # not just in the UI, per "authenticated owner... immediately before
    # sending" (P0-03's own acceptance criteria, which this equally
    # protects, since it's the same call site).
    if intent["user_id"] != calling_user_id:
        raise PermissionError(
            f"trade_intent {trade_intent_id} belongs to user {intent['user_id']}, "
            f"not the calling user {calling_user_id} — refusing to authorize."
        )

    # Real bug found 2026-09-22 (external review, P0-03, T10): this used to
    # be a plain read-then-later-write with no atomic claim in between —
    # two concurrent authorize() calls for the SAME trade_intent_id (a
    # double-click, two open browser tabs, a retry racing the original)
    # could BOTH read status=="AWAITING_AUTHORIZATION" and BOTH proceed to
    # place a real broker order, since the status update only happened at
    # the very end, after the broker call. An UPDATE ... WHERE status =
    # 'AWAITING_AUTHORIZATION' is atomic at the database level (exactly
    # one concurrent caller's UPDATE can match the WHERE clause and change
    # a row still in that state) — whichever call's rowcount comes back 1
    # has genuinely won the claim; every other concurrent call gets 0 and
    # returns SKIPPED immediately, never touching the broker. "AUTHORIZING"
    # is a new transient status so a claimed-but-not-yet-resolved intent
    # correctly disappears from list_pending() (which only shows
    # AWAITING_AUTHORIZATION) while being processed, rather than looking
    # like it's still waiting for a decision.
    with engine.begin() as conn:
        claim = conn.execute(
            update(trade_intents_table)
            .where(
                trade_intents_table.c.id == trade_intent_id,
                trade_intents_table.c.status == "AWAITING_AUTHORIZATION",
            )
            .values(status="AUTHORIZING")
        )
        claimed = claim.rowcount == 1
    if not claimed:
        return AuthorizationResult(
            decision="SKIPPED",
            order_result=None,
            detail=f"intent status is '{intent['status']}', not AWAITING_AUTHORIZATION — already handled "
                   "(or another concurrent request just claimed it)",
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

    def _release_claim() -> None:
        """Reverts the AUTHORIZING claim back to AWAITING_AUTHORIZATION —
        used only for recoverable pre-broker-call failures (no live price,
        price drifted, fresh risk re-check no longer clears) so the intent
        remains retryable next cycle/click, same as it always has been,
        rather than getting stuck in a transient state forever."""
        with engine.begin() as conn:
            conn.execute(
                update(trade_intents_table)
                .where(trade_intents_table.c.id == trade_intent_id)
                .values(status="AWAITING_AUTHORIZATION")
            )

    with engine.connect() as conn:
        risk = conn.execute(
            select(risk_decisions_table)
            .where(risk_decisions_table.c.trade_intent_id == trade_intent_id)
            .order_by(risk_decisions_table.c.id.desc())
        ).mappings().first()
    if risk is None or not risk["approved"] or not risk["size_units"]:
        raise ValueError(f"trade_intent {trade_intent_id} has no approved, sized risk_decision")
    # Real bug found 2026-09-22 (external review, P0-03): unconditional
    # int() truncated ANY fractional crypto size to a whole number —
    # risk_governor.evaluate()'s own sizing gate deliberately keeps crypto
    # fractional (RiskDecision.size_units' own docstring: "a whole BTC/ETH
    # costs tens of thousands of dollars"), so a real 0.25 BTC approval
    # either silently sent an 0-unit order (int(0.25) == 0, rejected or a
    # no-op at the broker) or a wildly wrong quantity for anything >= 1.0.
    # Forex/equities still truncate to a whole unit/share, matching how
    # they actually trade.
    size_units = risk["size_units"] if "/" in intent["instrument"] else int(risk["size_units"])

    prices = await broker.get_current_prices([intent["instrument"]])
    if not prices:
        _release_claim()
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
            _release_claim()
            return AuthorizationResult(
                "APPROVED", None,
                f"Price drifted {drift:.5f} since the signal was generated "
                f"(vs {PRICE_DRIFT_STOP_RATIO:.0%} of the {stop_distance:.5f} stop distance) — "
                "order NOT sent. The setup this signal reasoned about may no longer hold; "
                "wait for a fresh signal next cycle.",
            )

    # Real bug found 2026-09-22 (external review, P0-03): everything above
    # only re-checks whether the SIGNAL is still good (price drift); the
    # ACCOUNT could have changed just as much in the meantime purely from
    # OTHER trades — kill switch tripped, another position pushed past
    # MAX_CONCURRENT_POSITIONS or the correlation cap — none of which used
    # to be re-verified before sending an order built from a risk_decision
    # that could be hours old. See risk_governor.revalidate_before_
    # submission's own docstring for the full design rationale (including
    # why it deliberately does NOT re-derive confidence-based sizing).
    broker_kind = broker_kind_for(intent["instrument"])
    is_paper = isinstance(broker, PaperBroker)
    account_state = await broker.account_state()
    positions_raw = await broker.positions()
    # PaperBroker has no list_instruments() (it only ever simulates OANDA
    # trades against its own DB ledger, never a real broker's instrument
    # catalog — see its module docstring) — _build_usd_conversion_rates
    # would raise AttributeError for it. Guarded the same way
    # compute_correlated_stop_risk already is just below.
    usd_rates = (
        await _build_usd_conversion_rates(broker, [intent["instrument"]])
        if broker_kind == "oanda" and not is_paper else {}
    )
    open_count, exposure = compute_exposure(positions_raw, broker_kind, execution_service.execution_mode, usd_rates)
    stop_risk = (
        {} if is_paper
        else await compute_correlated_stop_risk(broker, broker_kind, positions_raw, execution_service.execution_mode, usd_rates)
    )
    reconciliation_ok = await execution_service.reconcile()
    revalidation = risk_governor.revalidate_before_submission(
        engine,
        user_id=intent["user_id"],
        instrument=intent["instrument"],
        action=intent["action"],
        account_nav=account_state.nav,
        current_price=current_price,
        open_position_count=open_count,
        open_positions_usd_direction=exposure,
        open_positions_stop_risk_usd=stop_risk,
        approved_size_units=size_units,
        reconciliation_ok=reconciliation_ok,
        now=now,
    )
    if not revalidation.approved:
        _release_claim()
        return AuthorizationResult(
            "APPROVED", None,
            f"Cleared risk at signal time, but no longer clears at submission time: "
            f"{revalidation.reason} — order NOT sent.",
        )
    size_units = revalidation.size_units

    tp_distance = intent["take_profit_distance"]
    if intent["action"] == "BUY":
        stop_price = current_price - stop_distance if stop_distance else None
        target_price = current_price + tp_distance if tp_distance else None
    else:
        stop_price = current_price + stop_distance if stop_distance else None
        target_price = current_price - tp_distance if tp_distance else None

    # Real bug found 2026-09-22 (external review, P0-03): client_order_id
    # was left to ExecutionService.execute()'s own default, which mints a
    # FRESH random uuid EVERY call — so a retried/double-clicked
    # authorize() for the same trade_intent_id got a different
    # client_order_id each time, completely defeating BOTH this system's
    # own orders_fills idempotency dedup (keyed on client_order_id) AND
    # the broker's own native duplicate-client-order-id rejection (OANDA's
    # clientExtensions.id, Alpaca's client_order_id — both reject/return-
    # existing rather than double-submit on a genuine repeat). A stable
    # ID derived from trade_intent_id (a unique DB primary key) makes
    # every retry of the SAME intent collide at both layers instead of
    # silently placing a second real order.
    result = await execution_service.execute(
        instrument=intent["instrument"],
        action=intent["action"],
        size_units=size_units,
        stop_loss_price=stop_price,
        take_profit_price=target_price,
        client_order_id=f"intent-{trade_intent_id}",
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

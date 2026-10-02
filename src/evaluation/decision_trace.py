"""Equity V2 Phase 17 — observability: full per-decision reproducibility.

Answers the brief's own literal test ("why did the system buy NVDA at
10:32:17") by joining every table a single decision actually touches,
already stored by existing code, into one structured trace — this module
adds no new data collection, only a query layer over what every earlier
phase (and the pre-existing forex pipeline) already persists:

trade_intents (the decision itself: action/confidence/regime/explanation/
key_drivers/data_freshness, written by src/run_loop.py) -> predictions
(each component's own p_up/expected_move/confidence for that same
instrument/horizon/time, written alongside the intent) -> risk_decisions
(approved/reason/size/gates_json, a DIRECT trade_intent_id foreign key)
-> orders_fills (the real broker request/response, joined via
client_order_id = f"intent-{trade_intent_id}" — the exact convention
src/run_loop.py and src/authorization/service.py both already use) ->
trade_outcomes (the eventual realized result, a trade_intent_id foreign
key, nullable since not every intent is ever actually filled).

Honest, disclosed limitation, not engineered around: model_registry only
tracks the META-MODEL's own version/deployment history (MODEL_NAME =
"meta_model") — the PRICE model is retrained fresh every single cycle per
(instrument, horizon) and never persisted with a version identifier at
all (confirmed by reading src/run_loop.py's build_price_models and
src/models/price_model.py — there is no "version" concept for it). This
trace reports the meta-model's CURRENTLY deployed version as the closest
available proxy, honestly labeled as such — it is NOT a guarantee that
this exact version was active at the historical decision's own moment,
since model_registry has no historical point-in-time tracking. A future
phase could add that; this one does not fabricate precision that doesn't
exist in the underlying data.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.data.db import model_registry as model_registry_table
from src.data.db import orders_fills as orders_fills_table
from src.data.db import predictions as predictions_table
from src.data.db import risk_decisions as risk_decisions_table
from src.data.db import trade_intents as trade_intents_table
from src.data.db import trade_outcomes as trade_outcomes_table

META_MODEL_NAME = "meta_model"


@dataclass(frozen=True)
class DecisionTrace:
    trade_intent_id: int
    found: bool
    time: object | None = None
    instrument: str | None = None
    action: str | None = None
    horizon: str | None = None
    confidence: float | None = None
    regime: str | None = None
    status: str | None = None
    explanation: str | None = None
    key_drivers: list | None = None
    contrary_evidence: list | None = None
    data_freshness: dict | None = None
    reference_price: float | None = None
    component_predictions: tuple[dict, ...] = ()
    risk_decision: dict | None = None
    order_fill: dict | None = None
    outcome: dict | None = None
    meta_model_currently_deployed_version: str | None = None


def _parse_json_field(raw: str | None) -> object | None:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw  # genuinely malformed JSON -- return the raw string rather than silently losing it


def build_decision_trace(engine: Engine, trade_intent_id: int) -> DecisionTrace:
    with engine.connect() as conn:
        intent = conn.execute(
            select(trade_intents_table).where(trade_intents_table.c.id == trade_intent_id)
        ).mappings().first()
        if intent is None:
            return DecisionTrace(trade_intent_id=trade_intent_id, found=False)

        predictions = conn.execute(
            select(predictions_table).where(
                predictions_table.c.instrument == intent["instrument"],
                predictions_table.c.horizon == intent["horizon"],
                predictions_table.c.time == intent["time"],
            )
        ).mappings().all()

        risk = conn.execute(
            select(risk_decisions_table).where(risk_decisions_table.c.trade_intent_id == trade_intent_id)
        ).mappings().first()

        order = conn.execute(
            select(orders_fills_table).where(orders_fills_table.c.client_order_id == f"intent-{trade_intent_id}")
        ).mappings().first()

        outcome = conn.execute(
            select(trade_outcomes_table).where(trade_outcomes_table.c.trade_intent_id == trade_intent_id)
        ).mappings().first()

        meta_version_row = conn.execute(
            select(model_registry_table.c.version)
            .where(model_registry_table.c.name == META_MODEL_NAME, model_registry_table.c.deployed.is_(True))
            .order_by(model_registry_table.c.trained_at.desc())
            .limit(1)
        ).first()

    return DecisionTrace(
        trade_intent_id=trade_intent_id,
        found=True,
        time=intent["time"],
        instrument=intent["instrument"],
        action=intent["action"],
        horizon=intent["horizon"],
        confidence=intent["confidence"],
        regime=intent["regime"],
        status=intent["status"],
        explanation=intent["explanation"],
        key_drivers=_parse_json_field(intent["key_drivers_json"]),
        contrary_evidence=_parse_json_field(intent["contrary_evidence_json"]),
        data_freshness=_parse_json_field(intent["data_freshness_json"]),
        reference_price=intent["reference_price"],
        component_predictions=tuple(dict(p) for p in predictions),
        risk_decision=(dict(risk) | {"gates": _parse_json_field(risk["gates_json"])}) if risk else None,
        order_fill=dict(order) if order else None,
        outcome=dict(outcome) if outcome else None,
        meta_model_currently_deployed_version=meta_version_row.version if meta_version_row else None,
    )


def explain_decision(trace: DecisionTrace) -> str:
    """A short, human-readable narrative from the trace -- the literal
    "why did the system do X at time T" answer the brief asks for,
    assembled from already-real fields, never inventing a reason the
    trace itself doesn't actually contain."""
    if not trace.found:
        return f"No trade_intent with id={trace.trade_intent_id} exists."

    lines = [
        f"At {trace.time}, the system evaluated {trace.instrument} on the {trace.horizon} horizon "
        f"and decided {trace.action} with confidence {trace.confidence:.2f} (regime: {trace.regime or 'unknown'})."
    ]
    if trace.explanation:
        lines.append(f"Stated reasoning: {trace.explanation}")
    if trace.component_predictions:
        parts = [f"{p['component']}={p.get('p_up')}" for p in trace.component_predictions]
        lines.append(f"Component predictions: {', '.join(parts)}.")
    if trace.risk_decision is not None:
        approved = trace.risk_decision["approved"]
        lines.append(
            f"Risk governor {'APPROVED' if approved else 'REJECTED'}: {trace.risk_decision['reason']}"
            + (f" (size {trace.risk_decision['size_units']} units)" if approved else "")
        )
    else:
        lines.append("No risk_decisions row was ever recorded for this intent.")
    if trace.order_fill is not None:
        lines.append(f"Broker response: status={trace.order_fill['status']}, fill_price={trace.order_fill.get('fill_price')}.")
    elif trace.outcome is not None:
        # Real bug this guards against, caught live against a real historic
        # trade (a genuine +$8,902 NVDA win predating the f"intent-{id}"
        # client_order_id convention this join relies on): an outcome row
        # proves a real fill happened even when order_fill can't be joined
        # (an older/different client_order_id scheme) -- claiming "never
        # actually sent" here would be flatly contradicted by the outcome
        # recorded two lines later.
        lines.append(
            "No order_fill row could be joined (likely an older client_order_id "
            "convention) — but a real outcome was recorded, so a fill clearly did happen."
        )
    elif trace.risk_decision is not None and trace.risk_decision["approved"]:
        lines.append("Risk-approved, but no order_fill row exists — never actually sent to the broker.")
    if trace.outcome is not None:
        lines.append(f"Final outcome: {trace.outcome['outcome']}, realized P/L ${trace.outcome['realized_pl_usd']:+.2f}.")
    return " ".join(lines)

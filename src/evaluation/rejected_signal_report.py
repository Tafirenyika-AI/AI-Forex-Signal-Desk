"""AI Trading Desk V4 Priority 2 (docs/V4_ARCHITECTURE.md Section 14 gap):
"rejected-signal hypothetical outcome tracking" — did the risk governor's
rejections actually save money, or is it too conservative?

Investigating this (required before building anything, same discipline as
every other change this session) found the gap analysis in
docs/V4_ARCHITECTURE.md was WRONG on this specific point: a NEW
`rejected_signal_outcomes` table + a NEW scheduled re-pricing job would
have duplicated work that already exists. src/scripts/evaluate_challengers.py
(the real-content-independent "champion" scoring path) already re-prices
EVERY trade_intent with action != NO_TRADE at its own horizon's close and
records hit/move_in_favor in `signal_evaluations` with source="champion" —
regardless of whether that trade_intent was ever risk-approved. Confirmed
live against production 2026-10-08: 8,853 of 10,778 real RISK_REJECTED
signals already have a scored champion signal_evaluations row.

What was genuinely missing is a way to SEGMENT that already-computed data
by risk-decision outcome/reason — this module is that query layer, not a
new data pipeline. Research-only: nothing here feeds back into any live
risk/sizing decision.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import case, func, select
from sqlalchemy.engine import Engine

from src.data.db import risk_decisions as risk_decisions_table
from src.data.db import signal_evaluations as signal_evaluations_table
from src.data.db import trade_intents as trade_intents_table


@dataclass(frozen=True)
class RejectionOutcomeGroup:
    approved: bool
    reason: str
    n: int
    hit_rate: float | None
    mean_move_in_favor: float | None


def rejected_signal_segmented_report(engine: Engine, user_id: int | None = None) -> list[RejectionOutcomeGroup]:
    """One group per distinct risk_decisions.reason, each scored against the
    real, already-computed "would this direction have been right by
    horizon expiry" outcome from signal_evaluations (source="champion").
    Excludes NO_TRADE decisions (risk_decisions.reason == "decision was
    NO_TRADE") — those were never a real signal to reject in the first
    place and never get a champion evaluation row to join against."""
    with engine.connect() as conn:
        query = (
            select(
                risk_decisions_table.c.approved,
                risk_decisions_table.c.reason,
                func.count().label("n"),
                # Boolean AVG() isn't valid in Postgres (unlike SQLite's
                # integer-backed booleans), and Postgres also refuses a
                # direct bool->float CAST -- a CASE expression is the one
                # form that works on both dialects.
                func.avg(case((signal_evaluations_table.c.hit, 1.0), else_=0.0)).label("hit_rate"),
                func.avg(signal_evaluations_table.c.move_in_favor).label("mean_move_in_favor"),
            )
            .select_from(
                risk_decisions_table.join(
                    trade_intents_table, trade_intents_table.c.id == risk_decisions_table.c.trade_intent_id,
                ).join(
                    signal_evaluations_table,
                    (signal_evaluations_table.c.trade_intent_id == risk_decisions_table.c.trade_intent_id)
                    & (signal_evaluations_table.c.source == "champion"),
                )
            )
            .where(trade_intents_table.c.action != "NO_TRADE")
            .group_by(risk_decisions_table.c.approved, risk_decisions_table.c.reason)
            .order_by(func.count().desc())
        )
        if user_id is not None:
            query = query.where(trade_intents_table.c.user_id == user_id)
        rows = conn.execute(query).all()

    return [
        RejectionOutcomeGroup(
            approved=bool(r.approved), reason=r.reason, n=r.n,
            hit_rate=float(r.hit_rate) if r.hit_rate is not None else None,
            mean_move_in_favor=float(r.mean_move_in_favor) if r.mean_move_in_favor is not None else None,
        )
        for r in rows
    ]

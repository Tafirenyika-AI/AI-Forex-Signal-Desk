"""Historical analog retrieval (Autonomous Upgrade Spec sec. 13, last
bullet): "Before a new trade, retrieve statistically similar historical
situations and compare outcomes."

"Similar" here means the same instrument, regime and action — the three
things this system already tags every trade_intent with (see
src/decision/fusion.py's regime routing, P7). Same self-relative
sample-size handling philosophy as crowding_score/regime detection
elsewhere in this project: with too few same-instrument matches, fall back
to matching on regime+action across all instruments before giving up.

Deliberately informational, not a gate: results are logged (trade_analogs
table) and shown in the dashboard/decision explanation, but never used to
widen a risk limit — the risk governor is the only thing allowed to touch
sizing (src/risk/governor.py's own docstring), and sec. 8 of the original
blueprint is explicit that "the prediction model is never permitted to
increase its own risk limit because it 'feels confident.'" A pile of past
wins is exactly that kind of feeling.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.data.db import trade_intents as trade_intents_table
from src.data.db import trade_outcomes as trade_outcomes_table
from src.memory.r_multiple import compute_r_multiple

MIN_SAMPLES_FOR_INSTRUMENT_MATCH = 5


@dataclass(frozen=True)
class AnalogSummary:
    basis: str  # instrument_regime_action / regime_action_fallback / insufficient_history
    matched_count: int
    win_rate: float | None
    avg_r_multiple: float | None
    avg_pl_usd: float | None


def _query_matches(engine: Engine, *, regime: str, action: str, instrument: str | None) -> list[dict]:
    with engine.connect() as conn:
        stmt = (
            select(
                trade_outcomes_table.c.realized_pl_usd,
                trade_outcomes_table.c.outcome,
                trade_outcomes_table.c.units,
                trade_outcomes_table.c.entry_price,
                trade_outcomes_table.c.instrument,
                trade_intents_table.c.stop_distance,
            )
            .join(trade_intents_table, trade_outcomes_table.c.trade_intent_id == trade_intents_table.c.id)
            .where(trade_intents_table.c.regime == regime, trade_intents_table.c.action == action)
        )
        if instrument is not None:
            stmt = stmt.where(trade_outcomes_table.c.instrument == instrument)
        rows = conn.execute(stmt).mappings().all()
    return [dict(r) for r in rows]


def _summarize(rows: list[dict], basis: str) -> AnalogSummary:
    n = len(rows)
    if n == 0:
        return AnalogSummary(basis="insufficient_history", matched_count=0, win_rate=None,
                              avg_r_multiple=None, avg_pl_usd=None)

    wins = sum(1 for r in rows if r["outcome"] == "WIN")
    win_rate = wins / n
    avg_pl_usd = sum(r["realized_pl_usd"] for r in rows) / n

    r_multiples = [
        rm for r in rows
        if (rm := compute_r_multiple(r["instrument"], r["entry_price"], r["stop_distance"], r["units"], r["realized_pl_usd"])) is not None
    ]
    avg_r_multiple = sum(r_multiples) / len(r_multiples) if r_multiples else None

    return AnalogSummary(basis=basis, matched_count=n, win_rate=win_rate,
                          avg_r_multiple=avg_r_multiple, avg_pl_usd=avg_pl_usd)


def find_similar_trades(engine: Engine, *, instrument: str, regime: str, action: str) -> AnalogSummary:
    same_instrument = _query_matches(engine, regime=regime, action=action, instrument=instrument)
    if len(same_instrument) >= MIN_SAMPLES_FOR_INSTRUMENT_MATCH:
        return _summarize(same_instrument, "instrument_regime_action")

    fallback = _query_matches(engine, regime=regime, action=action, instrument=None)
    if len(fallback) >= MIN_SAMPLES_FOR_INSTRUMENT_MATCH:
        return _summarize(fallback, "regime_action_fallback")

    # Too little history either way — say so honestly rather than reporting
    # a misleadingly precise stat off a handful of samples.
    return AnalogSummary(basis="insufficient_history", matched_count=len(fallback),
                          win_rate=None, avg_r_multiple=None, avg_pl_usd=None)

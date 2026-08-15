"""Per-instrument historical reliability (user-requested 2026-08-14): "the
main issue is to maximize profit [on] trading instruments which are
profitable." This project's risk governor may only ever shrink position
size below baseline, never grow it beyond (same invariant
CONFIDENCE_SIZE_FLOOR and REGIME_SIZE_MULTIPLIERS already hold to — see
src/risk/governor.py's own docstring) — so a strong track record leaves
sizing at the baseline the rest of the pipeline already computed, it
doesn't add to it; a poor or unproven one dampens it. This still achieves
"trade the profitable ones harder" in practice: weak/unproven instruments
get relatively smaller size, so the genuinely profitable ones end up
carrying more of the book's real risk, without ever inflating confidence
in something that hasn't earned it.

Guards against a real trap found live 2026-08-14: USD_TRY showed a
literal 100% hit rate across 306 real signal_evaluations — but every
single one of them was a BUY (confirmed directly against the data). That
is not two-sided directional skill, it is one persistent trend (the lira's
structural depreciation) that never happened to break during the
measurement window. A system that trusted that as "this pair trades well"
would be fully exposed the moment that trend reverses (an EM central-bank
intervention or geopolitical shock can move a pair like this 5-10%+ in
minutes) with zero evidence the model can call the other side of it at
all. A track record only counts as proven once both BUY and SELL have
real, independently-sized evidence — otherwise this is honestly "no
opinion yet", the same convention every other signal in this project uses
when it lacks enough data (see e.g. src/models/calibration.py's
MIN_SEGMENT_SAMPLES gate, which this mirrors).
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.data.db import signal_evaluations as signal_evaluations_table

# Matches MIN_SAMPLES already used project-wide (meta-model training,
# promotion gates) for "enough real evidence to act on."
MIN_TOTAL_SAMPLES = 30
# Matches calibration.py's MIN_SEGMENT_SAMPLES — the bar for trusting a
# narrower slice (here: one specific direction) rather than just the
# aggregate count.
MIN_SAMPLES_PER_DIRECTION = 15
# Same floor value CONFIDENCE_SIZE_FLOOR already uses elsewhere in this
# project's risk governor, for a consistent "never crush size below half
# of baseline from a single soft signal" vocabulary.
RELIABILITY_FLOOR = 0.5


def _multiplier_from_records(records: list[tuple[str, bool]]) -> tuple[float, str]:
    """Pure function over already-fetched (action, hit) rows — shared by
    instrument_reliability_multiplier() (one DB query, one instrument) and
    all_track_records() (one DB query, every instrument at once, so it
    must not re-query per instrument)."""
    n = len(records)
    if n < MIN_TOTAL_SAMPLES:
        return 1.0, f"only {n} scored signal(s) so far (need {MIN_TOTAL_SAMPLES}+) — no adjustment"

    buy_n = sum(1 for a, _ in records if a == "BUY")
    sell_n = n - buy_n
    if min(buy_n, sell_n) < MIN_SAMPLES_PER_DIRECTION:
        return 1.0, (
            f"one-sided track record (BUY={buy_n}, SELL={sell_n}, need {MIN_SAMPLES_PER_DIRECTION}+ each) "
            "— real hit rate may just reflect one persistent trend, not proven two-way skill — no adjustment"
        )

    hits = sum(1 for _, h in records if h)
    hit_rate = hits / n
    multiplier = max(RELIABILITY_FLOOR, min(1.0, hit_rate / 0.5))
    return multiplier, (
        f"hit_rate={hit_rate:.0%} over {n} two-sided signals (BUY={buy_n}, SELL={sell_n}) -> x{multiplier:.2f}"
    )


def instrument_reliability_multiplier(engine: Engine, user_id: int, instrument: str) -> tuple[float, str]:
    """(multiplier, detail). Self-relative to a 50% (chance) baseline —
    never exceeds 1.0, only ever dampens toward RELIABILITY_FLOOR as the
    instrument's own real hit rate falls toward or below chance, and only
    once genuine two-sided evidence exists (both BUY and SELL
    independently past MIN_SAMPLES_PER_DIRECTION). Below either sample-size
    bar, returns 1.0 (no adjustment) rather than guessing."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(signal_evaluations_table.c.action, signal_evaluations_table.c.hit)
            .where(
                signal_evaluations_table.c.user_id == user_id,
                signal_evaluations_table.c.instrument == instrument,
                signal_evaluations_table.c.source == "champion",
                signal_evaluations_table.c.hit.isnot(None),
            )
        ).fetchall()
    return _multiplier_from_records([(a, h) for a, h in rows])


@dataclass(frozen=True)
class InstrumentTrackRecord:
    instrument: str
    n: int
    buy_n: int
    sell_n: int
    hit_rate: float | None
    two_sided: bool
    multiplier: float
    detail: str


def all_track_records(engine: Engine, user_id: int) -> list[InstrumentTrackRecord]:
    """One row per instrument with at least one scored champion signal —
    for dashboard visibility. Not filtered by MIN_TOTAL_SAMPLES itself
    (that's applied to the *multiplier*, not to whether a row is shown) so
    the dashboard can honestly show "still accumulating evidence" rows
    too, not just already-decided ones."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                signal_evaluations_table.c.instrument,
                signal_evaluations_table.c.action,
                signal_evaluations_table.c.hit,
            ).where(
                signal_evaluations_table.c.user_id == user_id,
                signal_evaluations_table.c.source == "champion",
                signal_evaluations_table.c.hit.isnot(None),
            )
        ).fetchall()

    by_instrument: dict[str, list[tuple[str, bool]]] = {}
    for instrument, action, hit in rows:
        by_instrument.setdefault(instrument, []).append((action, hit))

    results = []
    for instrument, records in sorted(by_instrument.items()):
        n = len(records)
        buy_n = sum(1 for a, _ in records if a == "BUY")
        sell_n = n - buy_n
        hits = sum(1 for _, h in records if h)
        hit_rate = hits / n if n else None
        multiplier, detail = _multiplier_from_records(records)
        two_sided = min(buy_n, sell_n) >= MIN_SAMPLES_PER_DIRECTION and n >= MIN_TOTAL_SAMPLES
        results.append(InstrumentTrackRecord(
            instrument=instrument, n=n, buy_n=buy_n, sell_n=sell_n,
            hit_rate=hit_rate, two_sided=two_sided, multiplier=multiplier, detail=detail,
        ))
    return sorted(results, key=lambda r: r.multiplier)

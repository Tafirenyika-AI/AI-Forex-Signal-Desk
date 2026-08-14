"""Confidence calibration (Autonomous Upgrade Spec sec. 16): "A displayed
confidence number must be empirically calibrated. If signals labeled 70%
only succeed about 52% of the time under the defined outcome, the model is
overconfident."

Every probability forecast is already stored — trade_intents.confidence at
signal time, signal_evaluations.hit once the horizon elapses and the real
outcome is known (src/scripts/evaluate_challengers.py, built for P9). This
module is purely an analysis layer over data that already exists; it
collects no new data of its own.

Segmentation (by pair / horizon / regime) only reports a segment once it
clears MIN_SEGMENT_SAMPLES — same self-relative sample-size philosophy as
crowding_score/regime detection/the meta-model's own MIN_SAMPLES gate.
Below that floor a segment is honestly reported as "insufficient data"
rather than a calibration curve built on five points.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.data.db import signal_evaluations as signal_evaluations_table
from src.data.db import trade_intents as trade_intents_table

MIN_SEGMENT_SAMPLES = 15
N_BINS = 5  # coarser than the textbook 10 — this system's total sample count doesn't support finer bins yet


@dataclass(frozen=True)
class CalibrationBin:
    bin_low: float
    bin_high: float
    n: int
    mean_predicted_confidence: float
    observed_hit_rate: float


@dataclass(frozen=True)
class CalibrationReport:
    segment: str  # e.g. "champion" or "champion/EUR_USD" or "champion/EUR_USD/RANGE"
    n: int
    brier_score: float  # 0 (perfect) .. 1 (worst); 0.25 is the baseline for a constant 0.5 guesser
    bins: list[CalibrationBin]


def _fetch_scored_signals(engine: Engine, source: str) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                signal_evaluations_table.c.hit,
                signal_evaluations_table.c.instrument,
                signal_evaluations_table.c.horizon,
                trade_intents_table.c.confidence,
                trade_intents_table.c.regime,
            )
            .select_from(signal_evaluations_table)
            .join(trade_intents_table, trade_intents_table.c.id == signal_evaluations_table.c.trade_intent_id)
            .where(signal_evaluations_table.c.source == source)
        ).mappings().all()
    return [dict(r) for r in rows]


def brier_score(rows: list[dict]) -> float:
    if not rows:
        return float("nan")
    return sum((r["confidence"] - float(r["hit"])) ** 2 for r in rows) / len(rows)


def _reliability_bins(rows: list[dict], n_bins: int = N_BINS) -> list[CalibrationBin]:
    edges = [i / n_bins for i in range(n_bins + 1)]
    bins = []
    for i in range(n_bins):
        low, high = edges[i], edges[i + 1]
        in_bin = [r for r in rows if low <= r["confidence"] < high or (i == n_bins - 1 and r["confidence"] == high)]
        if not in_bin:
            continue
        bins.append(CalibrationBin(
            bin_low=low, bin_high=high, n=len(in_bin),
            mean_predicted_confidence=sum(r["confidence"] for r in in_bin) / len(in_bin),
            observed_hit_rate=sum(1 for r in in_bin if r["hit"]) / len(in_bin),
        ))
    return bins


def calibration_report(engine: Engine, source: str, *, instrument: str | None = None,
                        horizon: str | None = None, regime: str | None = None) -> CalibrationReport | None:
    rows = _fetch_scored_signals(engine, source)
    if instrument is not None:
        rows = [r for r in rows if r["instrument"] == instrument]
    if horizon is not None:
        rows = [r for r in rows if r["horizon"] == horizon]
    if regime is not None:
        rows = [r for r in rows if r["regime"] == regime]

    if len(rows) < MIN_SEGMENT_SAMPLES:
        return None

    segment = "/".join(p for p in [source, instrument, horizon, regime] if p)
    return CalibrationReport(
        segment=segment, n=len(rows), brier_score=brier_score(rows), bins=_reliability_bins(rows),
    )


def all_reports(engine: Engine, source: str) -> list[CalibrationReport]:
    """Aggregate report plus every pair/horizon/regime segment that clears
    MIN_SEGMENT_SAMPLES on its own — spec: "calibrate separately by pair,
    holding period and market regime where sample size permits." """
    rows = _fetch_scored_signals(engine, source)
    reports = []

    aggregate = calibration_report(engine, source)
    if aggregate:
        reports.append(aggregate)

    for instrument in sorted({r["instrument"] for r in rows}):
        r = calibration_report(engine, source, instrument=instrument)
        if r:
            reports.append(r)
    for horizon in sorted({r["horizon"] for r in rows}):
        r = calibration_report(engine, source, horizon=horizon)
        if r:
            reports.append(r)
    for regime in sorted({r["regime"] for r in rows if r["regime"]}):
        r = calibration_report(engine, source, regime=regime)
        if r:
            reports.append(r)

    return reports

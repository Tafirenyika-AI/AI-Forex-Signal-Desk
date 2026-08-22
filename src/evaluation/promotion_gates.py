"""Long-running OANDA demo evaluation and promotion gates (Autonomous
Upgrade Spec sec. 18): "Do not promote on win rate alone. Promotion
decisions should consider sample size, expectancy after costs, maximum
drawdown, tail losses, risk-adjusted return, calibration, regime
stability, execution quality, uptime, data quality and recovery from
failures."

This module can only ever report the current, honest state of these
metrics — it cannot make evidence exist faster than trades accumulate. No
phase promotion is ever automatic; a human reads this report and decides.
That is true even if every gate below shows green: spec's own phase table
(sec. 18) requires running "across many trades, multiple market regimes
and major scheduled events" (Phase D) before Demo Autonomous can even be
considered stable, and that is calendar time no code can substitute for.

Phases, per spec sec. 18:
  A Instrumentation   -> data/timestamps/order lifecycle/logging/restart/reconciliation verified
  B Shadow             -> signals generated, scored against real outcomes, no orders sent
  C Demo Autonomous    -> executing under strict risk limits in the practice account
  D Stability           -> many trades, multiple regimes, major events, post-trade attribution
  E Live Assist         -> live market analysis, human approves every order
  F Limited Live        -> small real capital, conservative limits
  G Scale                -> limits increased only from evidence

This system is at Phase C today (auto-execute has been running against the
real OANDA practice account throughout this project) — this module's job
is to honestly measure whether it has actually earned Phase D yet, not to
assume it has because the code exists.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.data.db import orders_fills as orders_fills_table
from src.data.db import risk_decisions as risk_decisions_table
from src.data.db import trade_outcomes as trade_outcomes_table
from src.models.calibration import calibration_report

# Same MIN_SAMPLES floor as everywhere else in this project (meta-model,
# challenger evaluation) — below this, a metric is reported as "not enough
# evidence yet," never as a pass or fail.
MIN_SAMPLES_FOR_GATE = 30

CURRENT_PHASE = "C_Demo_Autonomous"
TARGET_PHASE = "D_Stability"


@dataclass(frozen=True)
class GateCriterion:
    name: str
    description: str
    current_value: str
    passed: bool | None  # None = not enough evidence to judge yet


@dataclass(frozen=True)
class PromotionGateReport:
    current_phase: str
    target_phase: str
    criteria: list[GateCriterion]
    ready_for_promotion: bool  # True only if every criterion explicitly passed — never True on missing evidence


def _real_trade_outcomes(engine: Engine, user_id: int) -> list[dict]:
    # This module is explicitly the OANDA demo evaluation/promotion report
    # (see module docstring) — filtered to match, now that Alpaca trades
    # are tracked too (src/outcomes/alpaca_tracker.py) and would otherwise
    # get silently pooled into a report about OANDA's own phase readiness.
    with engine.connect() as conn:
        rows = conn.execute(
            select(trade_outcomes_table).where(
                trade_outcomes_table.c.trade_intent_id.is_not(None),
                trade_outcomes_table.c.user_id == user_id,
                trade_outcomes_table.c.broker == "oanda",
            )
        ).mappings().all()
    return [dict(r) for r in rows]


def _sample_size_criterion(trades: list[dict]) -> GateCriterion:
    n = len(trades)
    passed = n >= MIN_SAMPLES_FOR_GATE if n else None
    return GateCriterion(
        "sample_size", f"At least {MIN_SAMPLES_FOR_GATE} real linked trade outcomes",
        f"{n} real trades", passed if n else None,
    )


def _expectancy_criterion(trades: list[dict]) -> GateCriterion:
    n = len(trades)
    if n < MIN_SAMPLES_FOR_GATE:
        return GateCriterion("expectancy", "Positive average realized P&L per trade (after real spread/slippage)",
                              f"n={n}, insufficient for a stable read", None)
    mean_pl = sum(t["realized_pl_usd"] for t in trades) / n
    return GateCriterion(
        "expectancy", "Positive average realized P&L per trade (after real spread/slippage)",
        f"${mean_pl:+.2f} avg over {n} trades", mean_pl > 0,
    )


def _max_drawdown_criterion(trades: list[dict]) -> GateCriterion:
    n = len(trades)
    if n < MIN_SAMPLES_FOR_GATE:
        return GateCriterion("max_drawdown", "Maximum peak-to-trough drawdown stays bounded",
                              f"n={n}, insufficient for a stable read", None)
    ordered = sorted(trades, key=lambda t: t["closed_at"])
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in ordered:
        equity += t["realized_pl_usd"]
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    starting_balance = 10000.0  # this project's paper/demo starting balance convention
    dd_pct = max_dd / starting_balance
    return GateCriterion(
        "max_drawdown", "Max drawdown stays under 10% of starting balance (illustrative threshold, not a spec number)",
        f"${max_dd:.2f} ({dd_pct:.1%} of ${starting_balance:.0f} starting balance)", dd_pct < 0.10,
    )


def _tail_loss_criterion(trades: list[dict]) -> GateCriterion:
    n = len(trades)
    if n < MIN_SAMPLES_FOR_GATE:
        return GateCriterion("tail_losses", "Worst single trade loss stays bounded relative to average risk",
                              f"n={n}, insufficient for a stable read", None)
    losses = sorted(t["realized_pl_usd"] for t in trades if t["realized_pl_usd"] < 0)
    if not losses:
        return GateCriterion("tail_losses", "Worst single trade loss stays bounded", "no losing trades yet", True)
    worst = losses[0]
    mean_loss = sum(losses) / len(losses)
    ratio = worst / mean_loss if mean_loss else 1.0
    return GateCriterion(
        "tail_losses", "Worst single loss isn't a wild outlier vs the average loss (<5x)",
        f"worst=${worst:.2f} vs mean loss ${mean_loss:.2f} ({ratio:.1f}x)", ratio < 5.0,
    )


def _risk_adjusted_return_criterion(trades: list[dict]) -> GateCriterion:
    n = len(trades)
    if n < MIN_SAMPLES_FOR_GATE:
        return GateCriterion("risk_adjusted_return", "Return per unit of P&L volatility (simple Sharpe-like ratio) is positive",
                              f"n={n}, insufficient for a stable read", None)
    pls = [t["realized_pl_usd"] for t in trades]
    mean_pl = sum(pls) / n
    variance = sum((p - mean_pl) ** 2 for p in pls) / n
    std_pl = math.sqrt(variance)
    ratio = mean_pl / std_pl if std_pl > 0 else 0.0
    return GateCriterion(
        "risk_adjusted_return", "Return per unit of P&L volatility (simple Sharpe-like ratio) is positive",
        f"{ratio:.3f} (mean ${mean_pl:+.2f} / std ${std_pl:.2f})", ratio > 0,
    )


def _calibration_criterion(engine: Engine) -> GateCriterion:
    report = calibration_report(engine, "champion")
    if report is None:
        return GateCriterion("calibration", "Champion's Brier score beats the constant-guess baseline (0.25)",
                              "not enough elapsed, scored signals yet", None)
    return GateCriterion(
        "calibration", "Champion's Brier score beats the constant-guess baseline (0.25)",
        f"Brier={report.brier_score:.3f} over n={report.n}", report.brier_score < 0.25,
    )


def _regime_stability_criterion(engine: Engine) -> GateCriterion:
    from src.models.regime import REGIME_TREND, REGIME_RANGE, REGIME_HIGH_VOL
    regimes_with_signal = [
        r for r in (REGIME_TREND, REGIME_RANGE, REGIME_HIGH_VOL)
        if calibration_report(engine, "champion", regime=r) is not None
    ]
    passed = len(regimes_with_signal) >= 2 if regimes_with_signal else None
    return GateCriterion(
        "regime_stability", "Calibration data exists in at least 2 distinct regimes, not concentrated in one",
        f"scored signals exist in: {', '.join(regimes_with_signal) or 'none yet'}", passed,
    )


def _execution_quality_criterion(engine: Engine, user_id: int) -> GateCriterion:
    with engine.connect() as conn:
        rows = conn.execute(
            select(orders_fills_table.c.status).where(orders_fills_table.c.user_id == user_id)
        ).all()
    n = len(rows)
    if n < 10:  # a much lower floor than MIN_SAMPLES_FOR_GATE — execution errors are worth seeing early
        return GateCriterion("execution_quality", "Order fill success rate stays high",
                              f"n={n} orders placed, too few to judge yet", None)
    filled = sum(1 for r in rows if r[0] == "FILLED")
    fill_rate = filled / n
    return GateCriterion(
        "execution_quality", "Order fill success rate stays high (>=95%)",
        f"{filled}/{n} filled ({fill_rate:.1%})", fill_rate >= 0.95,
    )


def _data_quality_criterion(engine: Engine, user_id: int) -> GateCriterion:
    with engine.connect() as conn:
        rows = conn.execute(
            select(risk_decisions_table.c.gates_json)
            .where(risk_decisions_table.c.user_id == user_id)
            .limit(1000)
        ).all()
    n = len(rows)
    if n < MIN_SAMPLES_FOR_GATE:
        return GateCriterion("data_quality", "Freshness gate rarely fails (<5% of decisions)",
                              f"n={n} risk decisions, insufficient for a stable read", None)
    freshness_failures = 0
    for (gates_json,) in rows:
        if not gates_json:
            continue
        for g in json.loads(gates_json):
            if g["name"] == "freshness" and not g["passed"]:
                freshness_failures += 1
                break
    failure_rate = freshness_failures / n
    return GateCriterion(
        "data_quality", "Freshness gate rarely fails (<5% of decisions)",
        f"{freshness_failures}/{n} decisions had stale data ({failure_rate:.1%})", failure_rate < 0.05,
    )


def _uptime_criterion(engine: Engine, user_id: int, now: datetime) -> GateCriterion:
    """Approximated from decision-cycle continuity rather than a live Task
    Scheduler query (this module has no PowerShell dependency) — gaps
    between consecutive trade_intents wider than 3x the run interval (5 min
    default, see run_loop.py) suggest the loop stopped, e.g. the real
    Windows-update interruption this project hit earlier."""
    from src.data.db import trade_intents as trade_intents_table
    with engine.connect() as conn:
        times = [
            r[0] for r in conn.execute(
                select(trade_intents_table.c.time)
                .where(trade_intents_table.c.time >= now - timedelta(days=7),
                       trade_intents_table.c.user_id == user_id)
                .order_by(trade_intents_table.c.time)
            ).all()
        ]
    if len(times) < 10:
        return GateCriterion("uptime", "No decision-cycle gaps wider than 15 minutes in the last 7 days",
                              f"only {len(times)} cycles logged in 7 days, insufficient for a stable read", None)
    times = [t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t for t in times]
    gaps = [(times[i + 1] - times[i]).total_seconds() for i in range(len(times) - 1)]
    max_gap_minutes = max(gaps) / 60 if gaps else 0.0
    long_gaps = sum(1 for g in gaps if g > 900)  # >15 min
    return GateCriterion(
        "uptime", "No decision-cycle gaps wider than 15 minutes in the last 7 days",
        f"longest gap {max_gap_minutes:.0f} min, {long_gaps} gap(s) over 15 min", long_gaps == 0,
    )


def build_report(engine: Engine, user_id: int, now: datetime | None = None) -> PromotionGateReport:
    """Trade outcomes, execution quality, data-quality and uptime are
    scoped to this one user's own account — those are properties of one
    person's real trading activity. Calibration and regime-stability stay
    pooled across every user's shadow-scored signals (src/models/
    calibration.py's `calibration_report(..., "champion", ...)`) — a
    deliberate choice, not an oversight: "is the champion's confidence
    score calibrated" is a property of the shared model, not of any one
    account, and pooling gets past the sample-size floor faster."""
    now = now or datetime.now(timezone.utc)
    trades = _real_trade_outcomes(engine, user_id)
    criteria = [
        _sample_size_criterion(trades),
        _expectancy_criterion(trades),
        _max_drawdown_criterion(trades),
        _tail_loss_criterion(trades),
        _risk_adjusted_return_criterion(trades),
        _calibration_criterion(engine),
        _regime_stability_criterion(engine),
        _execution_quality_criterion(engine, user_id),
        _data_quality_criterion(engine, user_id),
        _uptime_criterion(engine, user_id, now),
    ]
    ready = all(c.passed is True for c in criteria)
    return PromotionGateReport(
        current_phase=CURRENT_PHASE, target_phase=TARGET_PHASE, criteria=criteria, ready_for_promotion=ready,
    )

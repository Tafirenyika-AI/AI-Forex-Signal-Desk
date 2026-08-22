"""Automated post-trade attribution (Autonomous Upgrade Spec sec. 13):
"Perform automated post-trade attribution: which assumptions were correct,
which failed, and whether the loss was model error, execution error, noise,
regime change or unavoidable event risk."

v1 is rule-based, not a learned classifier — four independent checks each
either fire or don't (execution_error, event_risk, regime_change, plus a
confidence-based model_error read), every one of them is logged
(contributing_factors), and a single primary_reason is picked by priority
order among whichever fired. Same rule-based-first stance as this project's
other classifiers (source-quality scoring, hypothesis extraction, regime
detection) — a learned attribution model is a reasonable v2 once there's
enough labeled history to validate one against, not before.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import timedelta

import pandas as pd
from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.broker.oanda import OandaBroker
from src.data.db import central_bank_statements as central_bank_statements_table
from src.data.db import economic_surprises as economic_surprises_table
from src.data.db import orders_fills as orders_fills_table
from src.data.db import trade_intents as trade_intents_table
from src.memory.r_multiple import compute_r_multiple
from src.models.regime import classify_regime
from src.run_loop import HORIZON_CONFIGS, feature_ready_frame

# Execution-error: spread at entry eating more than this fraction of the
# planned stop distance is treated as a meaningful drag on the trade's own
# edge, not just ordinary cost-of-doing-business.
EXECUTION_SPREAD_STOP_RATIO = 0.25

# Model-error: only called out when the components genuinely agreed with
# the direction taken (the heuristic's own combined_confidence, already
# logged on the trade_intent) and the trade still lost — a confident,
# coherent, wrong call, not a marginal one where "noise" is the honest read.
MODEL_ERROR_MIN_CONFIDENCE = 0.5

# Regime-change lookback buffer before entry, generous enough that
# classify_regime's trailing-percentile warm-up (default lookback=250) has
# real history to work with even on H1 candles (250h ~= 10.4 days).
REGIME_LOOKBACK_BUFFER = timedelta(days=30)

_CENTRAL_BANK_CURRENCY = {"Fed": "USD"}  # only Fed is ingested (P5) — honestly scoped, not all 6


@dataclass(frozen=True)
class AttributionResult:
    trade_outcome_id: int
    trade_intent_id: int | None
    r_multiple: float | None
    primary_reason: str
    contributing_factors: list[str]


def _check_execution_error(spread: float | None, stop_distance: float | None) -> str | None:
    if not spread or not stop_distance or stop_distance <= 0:
        return None
    ratio = spread / stop_distance
    if ratio > EXECUTION_SPREAD_STOP_RATIO:
        return f"execution_error: entry spread {spread:.5f} was {ratio:.0%} of the {stop_distance:.5f} stop distance"
    return None


def _check_event_risk(
    engine: Engine, instrument: str, opened_at, closed_at,
) -> str | None:
    # No currency-calendar concept for non-forex instruments (Alpaca
    # equities/crypto) — nothing to attribute here.
    if "_" not in instrument:
        return None
    base, quote = instrument.split("_")
    currencies = {base, quote}
    with engine.connect() as conn:
        surprise_hits = conn.execute(
            select(economic_surprises_table.c.fred_event_name, economic_surprises_table.c.ff_event_time)
            .where(
                economic_surprises_table.c.currency.in_(currencies),
                economic_surprises_table.c.ff_event_time >= opened_at,
                economic_surprises_table.c.ff_event_time <= closed_at,
            )
        ).all()
        cb_hits = []
        for bank, cur in _CENTRAL_BANK_CURRENCY.items():
            if cur not in currencies:
                continue
            cb_hits.extend(
                conn.execute(
                    select(central_bank_statements_table.c.title)
                    .where(
                        central_bank_statements_table.c.central_bank == bank,
                        central_bank_statements_table.c.published_at >= opened_at,
                        central_bank_statements_table.c.published_at <= closed_at,
                    )
                ).all()
            )
    if surprise_hits:
        names = ", ".join(h[0] for h in surprise_hits)
        return f"event_risk: economic release(s) during holding window: {names}"
    if cb_hits:
        titles = ", ".join(h[0] for h in cb_hits)
        return f"event_risk: central bank statement during holding window: {titles}"
    return None


async def _check_regime_change(
    broker: OandaBroker, instrument: str, horizon: str, entry_regime: str | None, opened_at, closed_at,
) -> str | None:
    cfg = next((c for c in HORIZON_CONFIGS if c.label == horizon), None)
    if cfg is None or entry_regime is None:
        return None
    candles = await broker.get_candles_range(
        instrument, cfg.granularity, opened_at - REGIME_LOOKBACK_BUFFER, closed_at,
    )
    if len(candles) < 30:
        return None  # not enough history to trust a regime read here
    df = pd.DataFrame([dataclasses.asdict(c) for c in candles])
    featured = feature_ready_frame(df)
    if featured.empty:
        return None
    classified = classify_regime(featured)
    exit_regime = str(classified["regime"].iloc[-1])
    if exit_regime in ("SHOCK", "HIGH_VOLATILITY") and entry_regime not in ("SHOCK", "HIGH_VOLATILITY"):
        return f"regime_change: entered in {entry_regime}, regime had shifted to {exit_regime} by close"
    return None


async def attribute_trade(
    engine: Engine, broker: OandaBroker, trade_outcome_row: dict,
) -> AttributionResult:
    trade_outcome_id = trade_outcome_row["id"]
    trade_intent_id = trade_outcome_row["trade_intent_id"]

    trade_intent = None
    if trade_intent_id is not None:
        with engine.connect() as conn:
            trade_intent = conn.execute(
                select(trade_intents_table).where(trade_intents_table.c.id == trade_intent_id)
            ).mappings().first()

    stop_distance = trade_intent["stop_distance"] if trade_intent else None
    r_multiple = compute_r_multiple(
        trade_outcome_row["instrument"], trade_outcome_row["entry_price"], stop_distance,
        trade_outcome_row["units"], trade_outcome_row["realized_pl_usd"],
    )

    factors: list[str] = []

    fill = None
    if trade_outcome_row.get("client_order_id"):
        with engine.connect() as conn:
            fill = conn.execute(
                select(orders_fills_table).where(
                    orders_fills_table.c.client_order_id == trade_outcome_row["client_order_id"]
                )
            ).mappings().first()
    exec_error = _check_execution_error(fill["spread"] if fill else None, stop_distance)
    if exec_error:
        factors.append(exec_error)

    opened_at = trade_outcome_row.get("opened_at") or trade_outcome_row["closed_at"]
    event_risk = _check_event_risk(engine, trade_outcome_row["instrument"], opened_at, trade_outcome_row["closed_at"])
    if event_risk:
        factors.append(event_risk)

    regime_change = None
    if trade_intent is not None:
        try:
            regime_change = await _check_regime_change(
                broker, trade_outcome_row["instrument"], trade_intent["horizon"],
                trade_intent["regime"], opened_at, trade_outcome_row["closed_at"],
            )
        except Exception:  # noqa: BLE001 — attribution is best-effort, never blocks
            regime_change = None
    if regime_change:
        factors.append(regime_change)

    outcome = trade_outcome_row["outcome"]
    confidence = trade_intent["confidence"] if trade_intent else None
    if outcome == "WIN":
        primary_reason = "model_correct"
    elif outcome == "BREAKEVEN":
        primary_reason = "noise"
    else:  # LOSS
        if exec_error:
            primary_reason = "execution_error"
        elif event_risk:
            primary_reason = "event_risk"
        elif regime_change:
            primary_reason = "regime_change"
        elif confidence is not None and confidence >= MODEL_ERROR_MIN_CONFIDENCE:
            primary_reason = "model_error"
            factors.append(f"model_error: heuristic confidence was {confidence:.2f} — a confident, coherent call that still lost")
        else:
            primary_reason = "noise"

    return AttributionResult(
        trade_outcome_id=trade_outcome_id,
        trade_intent_id=trade_intent_id,
        r_multiple=r_multiple,
        primary_reason=primary_reason,
        contributing_factors=factors,
    )

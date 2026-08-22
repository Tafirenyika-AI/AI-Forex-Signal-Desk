"""Core Real-Time Workflow (blueprint sec. 13).

Modes (blueprint sec. 14 Phase 3/4):
    shadow  - full pipeline runs, decisions and risk gates are logged, no
              orders are ever sent. This is the safe default.
    paper   - orders go to the in-process PaperBroker (simulated fills
              against real live prices), but only after human sign-off.
    demo    - orders go to the real OANDA practice account, only after
              human sign-off. (config.py refuses to start at all if
              OANDA_ENVIRONMENT=live.)

Human-in-the-loop by default: for paper/demo, once a trade clears every risk
gate it is NOT executed automatically — it's marked AWAITING_AUTHORIZATION
and just sits there. Review and approve/reject it in the dashboard
(streamlit run src/dashboard/app.py), which records who approved it and
when in the `authorizations` table. Pass --auto-execute to skip this (loud
warning logged every cycle regardless).

--auto-execute on --mode demo is intentionally allowed: config.py already
refuses to start at all if OANDA_ENVIRONMENT=live, so "demo" here can only
ever mean OANDA's practice account — never real capital. This combination
exists specifically so outcome history can accumulate fast enough to train
a real calibrated meta-model (blueprint sec. 6's meta-model row —
src/models/train_meta_model.py) instead of the fixed heuristic blend
decision/fusion.py ships with. Auto-execute skips only the human click —
every other gate (freshness, spread, event, confidence, agreement,
correlation, sizing, daily-loss kill switch) still runs on every trade,
attended or not.

Multi-horizon by design (blueprint sec. 6.1): every cycle evaluates each pair
at 15m, 1h and 4h independently, with its own model, its own regime read, and
its own expiry. A day-trader checking every hour and a swing trader checking
once a day are both served every cycle — there is no single "the" signal per
pair, there's one per (pair, horizon), and each one is only ever as fresh as
its own horizon (src/authorization/service.py auto-expires stale ones rather
than letting a 15m read get authorized eight hours later).

Run from the project root with the venv active:
    python -m src.run_loop --mode shadow --once
    python -m src.run_loop --mode paper --interval 300
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import pickle
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import insert, select, update

from src.auth.service import UserTradingContext, active_trading_users
from src.broker.alpaca import AlpacaBroker
from src.broker.oanda import OandaBroker
from src.broker.registry import BrokerKind, asset_class_for, broker_kind_for
from src.config import Settings, load_settings
from src.challengers.definitions import Challenger, active_challengers
from src.data.db import (
    authorizations as authorizations_table,
    challenger_decisions as challenger_decisions_table,
    economic_events as economic_events_table,
    economic_surprises as economic_surprises_table,
    get_engine,
    market_indicators as market_indicators_table,
    market_prices as market_prices_table,
    news_events as news_events_table,
    predictions as predictions_table,
    risk_decisions as risk_decisions_table,
    trade_analogs as trade_analogs_table,
    trade_intents as trade_intents_table,
)
from src.decision.fusion import ComponentView, fuse, price_component_view
from src.execution.paper_broker import PaperBroker
from src.execution.service import ExecutionService
from src.features.engine import FEATURE_COLUMNS, add_forward_target, feature_ready_frame
from src.memory.analog_retrieval import find_similar_trades
from src.models.cross_market_model import pair_cross_market_score
from src.models.macro_model import pair_macro_score
from src.models.news_model import pair_news_score
from src.models.price_model import fit as fit_price_model, predict_proba_up
from src.models.train_meta_model import load_deployed_meta_model
from src.models.regime import classify_regime
from src.models.trading_sessions import (
    PriorSessionRange,
    compute_prior_session_range,
    current_session_state,
    cycle_cadence_ok,
    is_forex_open,
    is_nyse_open,
    pair_session_score,
)
from src.risk import governor as risk_governor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("run_loop")

PAIRS = ["EUR_USD", "GBP_USD", "USD_JPY", "USD_CAD", "AUD_USD"]
TRAIN_CANDLE_COUNT = 500


@dataclass(frozen=True)
class HorizonConfig:
    label: str
    granularity: str
    horizon_bars: int


# blueprint sec. 6.1's recommended v1 horizons. 1h and 4h share H1 candles
# (just a different forward-looking bar count), so they're fetched once and
# reused — only 15m needs its own M15 candle series.
HORIZON_CONFIGS = [
    HorizonConfig("15m", "M15", 1),
    HorizonConfig("1h", "H1", 1),
    HorizonConfig("4h", "H1", 4),
]

_GRANULARITY_MINUTES = {"M15": 15, "H1": 60}


def horizon_duration(cfg: HorizonConfig) -> timedelta:
    """Real wall-clock length of one horizon's bar count — used by
    src/scripts/evaluate_challengers.py to know when a signal's horizon has
    actually elapsed and it's fair to check what price did."""
    return timedelta(minutes=_GRANULARITY_MINUTES[cfg.granularity] * cfg.horizon_bars)


MODEL_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "model_cache"
# Every 5-minute cycle retraining every (instrument, horizon) from scratch
# was fine at 5 pairs by accident, not by design — real cost once the
# instrument list grows toward the full account universe (68 pairs verified
# live 2026-08-13 = up to 204 GBM fits/cycle otherwise). Retraining hourly
# instead of every cycle is a real accuracy/freshness tradeoff, not free —
# an hour-old model still predicts every cycle in between, just without a
# refit — but it's the honest cost of scaling instrument count without
# redesigning the whole training pipeline, and matches this project's
# established automation cadence for "retraining/evaluation" work (see
# ⚙️ Automation Center jobs, mostly hourly). Shared across every user, not
# per-user: the trained model itself has no per-user inputs (see
# src/models/price_model.py — pure technical features), so two users
# watching the same pair reuse one cached fit instead of training it twice.
MODEL_RETRAIN_INTERVAL = timedelta(hours=1)


def _model_cache_path(pair: str, horizon_label: str) -> Path:
    return MODEL_CACHE_DIR / f"{pair}_{horizon_label}.pkl"


def _load_cached_model(pair: str, horizon_label: str) -> object | None:
    path = _model_cache_path(pair, horizon_label)
    if not path.exists():
        return None
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    if datetime.now(timezone.utc) - mtime > MODEL_RETRAIN_INTERVAL:
        return None
    try:
        with open(path, "rb") as f:
            return pickle.load(f)
    except Exception:  # noqa: BLE001 — a corrupt/incompatible cache file should trigger a retrain, not crash the cycle
        logger.warning("Could not load cached model at %s, retraining", path, exc_info=True)
        return None


def _save_cached_model(pair: str, horizon_label: str, model: object) -> None:
    MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(_model_cache_path(pair, horizon_label), "wb") as f:
        pickle.dump(model, f)


async def build_price_models(
    broker, pairs: list[str], horizon_configs: list[HorizonConfig] = HORIZON_CONFIGS
) -> dict[tuple[str, str], object]:
    models: dict[tuple[str, str], object] = {}
    candle_cache: dict[tuple[str, str], list] = {}

    for pair in pairs:
        for cfg in horizon_configs:
            cached_model = _load_cached_model(pair, cfg.label)
            if cached_model is not None:
                models[(pair, cfg.label)] = cached_model
                continue

            cache_key = (pair, cfg.granularity)
            if cache_key not in candle_cache:
                candle_cache[cache_key] = await broker.get_candles(
                    pair, cfg.granularity, count=TRAIN_CANDLE_COUNT
                )
            candles = candle_cache[cache_key]
            if len(candles) < 100:
                logger.warning("Not enough %s candles to train a %s model for %s yet",
                                cfg.granularity, cfg.label, pair)
                continue

            df = pd.DataFrame([dataclasses.asdict(c) for c in candles])
            featured = feature_ready_frame(df)
            labeled = add_forward_target(featured, cfg.horizon_bars).dropna(subset=["target_up"])
            if len(labeled) < 60:
                logger.warning("Not enough labeled rows to train a %s model for %s yet", cfg.label, pair)
                continue

            models[(pair, cfg.label)] = fit_price_model(labeled)
            _save_cached_model(pair, cfg.label, models[(pair, cfg.label)])
            logger.info("Trained %s/%s price model on %d rows", pair, cfg.label, len(labeled))

    return models


async def _build_usd_conversion_rates(broker, instrument_list: list[str]) -> dict[str, float]:
    """One real batch price fetch of whichever direct-to-USD pair each
    currency in this cycle's instrument list actually has on this account
    (via broker.list_instruments(), so it adapts to whatever that specific
    account offers rather than assuming one fixed set) — makes
    risk_governor.usd_value_per_unit() work for cross pairs with no literal
    USD leg (e.g. AUD_CAD), which is the majority of pairs once the
    instrument list goes beyond the original 5 USD-pairs."""
    currencies: set[str] = set()
    for pair in instrument_list:
        if "_" not in pair:
            continue
        base, quote = pair.split("_")
        currencies.add(base)
        currencies.add(quote)
    currencies.discard("USD")
    if not currencies:
        return {}

    instrument_metadata = await broker.list_instruments()
    pair_for_currency: dict[str, tuple[str, bool]] = {}
    for cur in currencies:
        if f"{cur}_USD" in instrument_metadata:
            pair_for_currency[cur] = (f"{cur}_USD", True)
        elif f"USD_{cur}" in instrument_metadata:
            pair_for_currency[cur] = (f"USD_{cur}", False)
        else:
            logger.warning("No direct USD pair found for %s on this account — "
                            "cross pairs involving it will fail to size", cur)

    needed_instruments = sorted({p for p, _ in pair_for_currency.values()})
    if not needed_instruments:
        return {}
    prices = await broker.get_current_prices(needed_instruments)
    price_by_instrument = {p.instrument: p.mid for p in prices}

    rates: dict[str, float] = {}
    for cur, (instrument, quote_is_usd) in pair_for_currency.items():
        mid = price_by_instrument.get(instrument)
        if not mid or mid <= 0:
            continue
        rates[cur] = mid if quote_is_usd else (1.0 / mid)
    return rates


def _recent_median_spread(engine, instrument: str) -> float | None:
    stmt = (
        select(market_prices_table.c.bid, market_prices_table.c.ask)
        .where(market_prices_table.c.instrument == instrument)
        .order_by(market_prices_table.c.id.desc())
        .limit(200)
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).fetchall()
    if not rows:
        return None
    spreads = [ask - bid for bid, ask in rows]
    return float(pd.Series(spreads).median())


def _load_events(engine, table) -> list[dict]:
    """SQLite round-trips DateTime(timezone=True) columns as naive datetimes
    (it doesn't actually persist tzinfo) even though every writer in this
    codebase stores UTC. Reattach UTC on read rather than let naive/aware
    subtraction crash downstream — the data is UTC either way."""
    with engine.connect() as conn:
        rows = conn.execute(select(table)).mappings().all()
    result = []
    for r in rows:
        row = dict(r)
        for key, value in row.items():
            if isinstance(value, datetime) and value.tzinfo is None:
                row[key] = value.replace(tzinfo=timezone.utc)
        result.append(row)
    return result


def _normalized_positions(positions_raw: list[dict], broker_kind: BrokerKind, execution_mode: str):
    """Shape of a raw position dict depends on which BROKER returned it, not
    on execution_mode — execution_mode only distinguishes OANDA's real
    position shape from PaperBroker's simulated one (PaperBroker only ever
    wraps OANDA, never Alpaca, so that distinction still only matters
    within the OANDA branch). Alpaca's own shape (from AlpacaBroker.
    positions(), src/broker/alpaca.py) is a third, broker-specific case."""
    for p in positions_raw:
        if broker_kind == "alpaca":
            instrument = p["symbol"]
            qty = float(p["qty"])
            net_units = qty if p.get("side") == "long" else -qty
            ref_price = float(p.get("avg_entry_price") or 0)
        elif execution_mode == "paper":
            instrument = p["instrument"]
            net_units = p["units"]
            ref_price = p["avg_price"]
        else:
            instrument = p["instrument"]
            long_units = float(p.get("long", {}).get("units", "0") or 0)
            short_units = float(p.get("short", {}).get("units", "0") or 0)
            net_units = long_units + short_units
            ref_price = float(
                p.get("long", {}).get("averagePrice")
                or p.get("short", {}).get("averagePrice")
                or 0
            )
        if net_units == 0:
            continue
        yield instrument, net_units, ref_price


def compute_exposure(
    positions_raw: list[dict], broker_kind: BrokerKind, execution_mode: str,
    usd_rates: dict[str, float] | None = None,
) -> tuple[int, dict[str, float]]:
    open_count = 0
    exposure = {"long_usd": 0.0, "short_usd": 0.0, "equity_long": 0.0, "equity_short": 0.0,
                "crypto_long": 0.0, "crypto_short": 0.0}
    for instrument, net_units, ref_price in _normalized_positions(positions_raw, broker_kind, execution_mode):
        open_count += 1
        action = "BUY" if net_units > 0 else "SELL"
        key = risk_governor.usd_direction_of_trade(instrument, action)
        if key == "no_usd_leg":
            continue  # no direct USD-denominated exposure to attribute — see governor.py's own docstring
        try:
            notional = abs(net_units) * risk_governor.usd_value_per_unit(instrument, ref_price or 1.0, usd_rates)
        except ValueError:
            notional = 0.0
        exposure[key] = exposure.get(key, 0.0) + notional
    return open_count, exposure


async def _evaluate_one_horizon(
    *,
    broker,
    engine,
    user_id: int,
    execution_service: ExecutionService,
    execution_mode: str,
    pair: str,
    cfg: HorizonConfig,
    model: object,
    candles: list,
    price,
    price_age_seconds: float,
    macro_score: float,
    macro_conf: float,
    news_score: float,
    news_conf: float,
    cross_market_score: float,
    cross_market_conf: float,
    session_score: float,
    session_conf: float,
    london_range: PriorSessionRange | None,
    calendar_covers_currency: bool,
    upcoming_tier1_event: bool,
    meta_model,
    account_state,
    open_count: int,
    exposure: dict[str, float],
    recent_spread: float | None,
    reconciliation_ok: bool,
    allow_unverified_event_risk: bool,
    auto_execute: bool,
    now: datetime,
    economic_surprises: list[dict],
    challengers: list[Challenger],
    usd_rates: dict[str, float],
) -> None:
    df = pd.DataFrame([dataclasses.asdict(c) for c in candles])
    featured = feature_ready_frame(df)
    if featured.empty:
        logger.info("%s/%s: not enough history for features yet", pair, cfg.label)
        return

    classified = classify_regime(featured)
    last_row = classified.iloc[[-1]]
    regime = str(last_row["regime"].iloc[0])
    atr_14 = float(last_row["atr_14"].iloc[0])
    candle_time = last_row["time"].iloc[0]
    candle_age_seconds = (now - candle_time.to_pydatetime()).total_seconds()

    p_up = float(predict_proba_up(model, last_row)[0])

    component_views = [
        price_component_view(p_up),
        ComponentView("macro", macro_score, macro_conf),
        ComponentView("cross_market", cross_market_score, cross_market_conf),
        ComponentView("news", news_score, news_conf),
        ComponentView("session", session_score, session_conf),
    ]
    data_freshness = {"price": price_age_seconds, "candles": candle_age_seconds}

    decision = fuse(
        instrument=pair,
        horizon=cfg.label,
        regime=regime,
        component_views=component_views,
        current_price=price.mid,
        atr_14=atr_14,
        data_freshness=data_freshness,
        now=now,
        meta_model=meta_model,
    )

    with engine.begin() as conn:
        for view in decision.component_views:
            conn.execute(
                insert(predictions_table),
                {
                    "time": now,
                    "instrument": pair,
                    "horizon": cfg.label,
                    "component": view.name,
                    "p_up": view.detail.get("p_up"),
                    "expected_move": None,
                    "confidence": view.confidence,
                    "raw_json": json.dumps({"score": view.score, **view.detail}),
                },
            )
        intent_result = conn.execute(
            insert(trade_intents_table).values(
                user_id=user_id,
                time=now,
                instrument=pair,
                action=decision.action,
                confidence=decision.confidence,
                horizon=cfg.label,
                regime=regime,
                key_drivers_json=json.dumps(decision.key_drivers),
                contrary_evidence_json=json.dumps(decision.contrary_evidence),
                entry_condition=decision.entry_condition,
                invalidation=decision.invalidation,
                stop_distance=decision.stop_distance,
                take_profit_distance=decision.take_profit_distance,
                target_logic=decision.target_logic,
                data_freshness_json=json.dumps(data_freshness),
                explanation=decision.explanation,
                status="PROPOSED",
                reference_price=price.mid,
                broker=broker_kind_for(pair),
            )
        )
        trade_intent_id = intent_result.inserted_primary_key[0]

    # Autonomous Upgrade Spec sec. 15: challengers see the exact same market
    # snapshot as the champion this cycle and run fully in shadow — fuse()
    # runs, but nothing here ever reaches the risk governor or a broker.
    # Runs even when the champion said NO_TRADE: a challenger disagreeing
    # with that is exactly the case shadow evaluation exists to catch.
    context = {
        "economic_surprises": economic_surprises, "pair": pair, "now": now,
        "london_range": london_range,
    }
    for challenger in challengers:
        extra_view = challenger.extra_view(context)
        if extra_view is None:
            continue  # nothing new to say this cycle — not logged as a no-op duplicate of the champion
        challenger_decision = fuse(
            instrument=pair,
            horizon=cfg.label,
            regime=regime,
            component_views=[*component_views, extra_view],
            current_price=price.mid,
            atr_14=atr_14,
            data_freshness=data_freshness,
            now=now,
            weights=challenger.weights,
            # No meta_model: it was calibrated on the champion's exact
            # feature set (see train_meta_model.py) — invalid for a
            # challenger with a different component shape.
        )
        with engine.begin() as conn:
            stmt = insert(challenger_decisions_table).values(
                user_id=user_id,
                trade_intent_id=trade_intent_id,
                challenger_name=challenger.name,
                instrument=pair,
                horizon=cfg.label,
                action=challenger_decision.action,
                confidence=challenger_decision.confidence,
                reference_price=price.mid,
                key_drivers_json=json.dumps(challenger_decision.key_drivers),
                computed_at=now,
            )
            conn.execute(stmt)
        logger.info(
            "%s/%s: challenger=%s -> %s conf=%.2f (champion=%s)",
            pair, cfg.label, challenger.name, challenger_decision.action,
            challenger_decision.confidence, decision.action,
        )

    # Autonomous Upgrade Spec sec. 13: "before a new trade, retrieve
    # statistically similar historical situations and compare outcomes."
    # Only meaningful once there's an actual candidate trade — skipped for
    # NO_TRADE, which is most cycles. Informational only (see
    # src/memory/analog_retrieval.py docstring) — never adjusts sizing.
    if decision.action != "NO_TRADE":
        analog = find_similar_trades(engine, instrument=pair, regime=regime, action=decision.action)
        with engine.begin() as conn:
            conn.execute(
                insert(trade_analogs_table),
                {
                    "user_id": user_id,
                    "trade_intent_id": trade_intent_id,
                    "basis": analog.basis,
                    "matched_count": analog.matched_count,
                    "win_rate": analog.win_rate,
                    "avg_r_multiple": analog.avg_r_multiple,
                    "avg_pl_usd": analog.avg_pl_usd,
                    "computed_at": now,
                },
            )
        if analog.win_rate is not None:
            logger.info(
                "%s/%s: %d historical analogs (%s) -> win_rate=%.0f%% avg_R=%s",
                pair, cfg.label, analog.matched_count, analog.basis, analog.win_rate * 100,
                f"{analog.avg_r_multiple:.2f}" if analog.avg_r_multiple is not None else "n/a",
            )

    logger.info(
        "%s/%s: regime=%s p_up=%.3f macro=%.2f(%.2f) xmkt=%.2f(%.2f) news=%.2f(%.2f) "
        "session=%.2f(%.2f) -> %s conf=%.2f",
        pair, cfg.label, regime, p_up, macro_score, macro_conf,
        cross_market_score, cross_market_conf, news_score, news_conf,
        session_score, session_conf, decision.action, decision.confidence,
    )

    if decision.action == "NO_TRADE":
        with engine.begin() as conn:
            conn.execute(
                insert(risk_decisions_table),
                {
                    "user_id": user_id,
                    "trade_intent_id": trade_intent_id,
                    "time": now,
                    "approved": False,
                    "reason": "decision was NO_TRADE",
                    "size_units": None,
                    "gates_json": None,
                },
            )
            conn.execute(
                update(trade_intents_table)
                .where(trade_intents_table.c.id == trade_intent_id)
                .values(status="NO_TRADE")
            )
        return

    effective_calendar_coverage = calendar_covers_currency or allow_unverified_event_risk
    component_scores = {v.name: v.score for v in decision.component_views}

    risk_decision = risk_governor.evaluate(
        engine,
        user_id=user_id,
        instrument=pair,
        action=decision.action,
        confidence=decision.confidence,
        stop_distance=decision.stop_distance,
        current_price=price.mid,
        current_spread=price.spread,
        recent_median_spread=recent_spread,
        data_freshness_seconds=data_freshness,
        account_balance=account_state.balance,
        account_nav=account_state.nav,
        open_position_count=open_count,
        open_positions_usd_direction=exposure,
        component_scores=component_scores,
        calendar_covers_currency=effective_calendar_coverage,
        upcoming_tier1_event_within_lockout=upcoming_tier1_event,
        reconciliation_ok=reconciliation_ok,
        regime=decision.regime,
        reference_price=price.mid,
        usd_rates=usd_rates,
        now=now,
    )

    with engine.begin() as conn:
        conn.execute(
            insert(risk_decisions_table),
            {
                "user_id": user_id,
                "trade_intent_id": trade_intent_id,
                "time": now,
                "approved": risk_decision.approved,
                "reason": risk_decision.reason,
                "size_units": risk_decision.size_units,
                "gates_json": json.dumps([dataclasses.asdict(g) for g in risk_decision.gates]),
            },
        )

    logger.info("%s/%s: risk_governor -> approved=%s reason=%s size=%s",
                pair, cfg.label, risk_decision.approved, risk_decision.reason, risk_decision.size_units)

    if not risk_decision.approved:
        with engine.begin() as conn:
            conn.execute(
                update(trade_intents_table)
                .where(trade_intents_table.c.id == trade_intent_id)
                .values(status="RISK_REJECTED")
            )
        return

    if execution_mode == "shadow":
        logger.info("%s/%s: shadow mode — decision logged, no order sent", pair, cfg.label)
        with engine.begin() as conn:
            conn.execute(
                update(trade_intents_table)
                .where(trade_intents_table.c.id == trade_intent_id)
                .values(status="SHADOW_LOGGED")
            )
        return

    if not auto_execute:
        # Human-in-the-loop by default (see module docstring): the trade
        # cleared every automated gate, but it does not get sent anywhere
        # until a person authorizes it in the dashboard.
        with engine.begin() as conn:
            conn.execute(
                update(trade_intents_table)
                .where(trade_intents_table.c.id == trade_intent_id)
                .values(status="AWAITING_AUTHORIZATION", execution_mode=execution_mode)
            )
        logger.info("%s/%s: risk-approved, AWAITING_AUTHORIZATION (open the dashboard to review)", pair, cfg.label)
        return

    logger.warning("%s/%s: --auto-execute is set — sending order WITHOUT human authorization", pair, cfg.label)
    stop_price = (
        price.mid - decision.stop_distance if decision.action == "BUY" else price.mid + decision.stop_distance
    )
    target_price = (
        price.mid + decision.take_profit_distance
        if decision.action == "BUY"
        else price.mid - decision.take_profit_distance
    )
    result = await execution_service.execute(
        instrument=pair,
        action=decision.action,
        size_units=risk_decision.size_units,
        stop_loss_price=stop_price,
        take_profit_price=target_price,
    )
    with engine.begin() as conn:
        conn.execute(
            update(trade_intents_table)
            .where(trade_intents_table.c.id == trade_intent_id)
            .values(status="AUTO_EXECUTED", execution_mode=execution_mode)
        )
        # Same audit trail an authorized trade gets, just a different actor —
        # this is also the only link the outcome tracker (src/outcomes/) has
        # from a closed broker trade back to the trade_intent that caused it.
        conn.execute(
            insert(authorizations_table),
            {
                "user_id": user_id,
                "trade_intent_id": trade_intent_id,
                "decision": "AUTO_APPROVED",
                "authorized_by": "system_auto_execute",
                "authorized_at": now,
                "notes": "sent via --auto-execute, no human review",
                "resulting_client_order_id": result.client_order_id,
            },
        )
    logger.info("%s/%s: execution -> status=%s order_id=%s", pair, cfg.label, result.status, result.broker_order_id)


async def evaluate_pair(
    *,
    broker,
    engine,
    user_id: int,
    execution_service: ExecutionService,
    execution_mode: str,
    price_models: dict[tuple[str, str], object],
    pair: str,
    economic_events: list[dict],
    news_events: list[dict],
    market_indicator_rows: list[dict],
    allow_unverified_event_risk: bool,
    auto_execute: bool,
    meta_model,
    now: datetime,
    economic_surprises: list[dict],
    challengers: list[Challenger],
    account_state,
    open_count: int,
    exposure: dict[str, float],
    reconciliation_ok: bool,
    usd_rates: dict[str, float],
    horizon_configs: list[HorizonConfig] = HORIZON_CONFIGS,
) -> None:
    """Evaluates every configured horizon for one pair. Portfolio-wide state
    (account, positions, exposure, reconciliation) is now fetched ONCE per
    user per cycle by the caller (_run_once_for_user) and passed in here —
    it's identical for every pair in that user's instrument list, so
    re-fetching it per pair was pure waste that only stayed cheap by
    accident at 5 pairs; real cost once the list grows toward the full
    68-pair account universe (verified live 2026-08-13). Only spread
    history is still genuinely pair-specific and fetched here."""
    prices = await broker.get_current_prices([pair])
    if not prices:
        logger.warning("%s: no live price available", pair)
        return
    price = prices[0]
    price_age_seconds = (now - price.time).total_seconds()

    macro_score, macro_conf = pair_macro_score(economic_events, pair, now)
    news_score, news_conf = pair_news_score(news_events, pair, now)
    cross_market_score, cross_market_conf = pair_cross_market_score(market_indicator_rows, pair)
    # Regional FX session scoring (London/NY/Tokyo/Sydney overlap) has no
    # equivalent for a single-exchange equity market or a 24/7 crypto pair —
    # "no opinion" for either, same convention every other component uses
    # when it has nothing to say, rather than forcing FX-shaped session
    # semantics onto instruments they don't apply to.
    is_forex = asset_class_for(pair) == "forex"
    if is_forex:
        session_score, session_conf = await pair_session_score(broker, pair, now, price.mid)
    else:
        session_score, session_conf = 0.0, 0.0
    # Only fetched during New York's own opening transition window (the
    # specific handoff the "NY reversal" challenger is about) — an extra
    # broker call every cycle for every pair, for a signal that only ever
    # matters in a ~45min/day window, would be pure waste the rest of the day.
    london_range = None
    if is_forex and "New_York" in current_session_state(now).just_opened:
        london_range = await compute_prior_session_range(broker, pair, "London", now)
    calendar_covers_currency, upcoming_tier1_event = risk_governor.check_calendar_event_risk(
        economic_events, pair, now
    )

    recent_spread = _recent_median_spread(engine, pair)

    candle_cache: dict[str, list] = {}
    for cfg in horizon_configs:
        model = price_models.get((pair, cfg.label))
        if model is None:
            logger.info("%s/%s: no trained price model yet, skipping", pair, cfg.label)
            continue

        if cfg.granularity not in candle_cache:
            candle_cache[cfg.granularity] = await broker.get_candles(
                pair, cfg.granularity, count=TRAIN_CANDLE_COUNT
            )
        candles = candle_cache[cfg.granularity]
        if not candles:
            logger.warning("%s/%s: no candles returned", pair, cfg.label)
            continue

        await _evaluate_one_horizon(
            broker=broker,
            engine=engine,
            user_id=user_id,
            execution_service=execution_service,
            execution_mode=execution_mode,
            pair=pair,
            cfg=cfg,
            model=model,
            candles=candles,
            price=price,
            price_age_seconds=price_age_seconds,
            macro_score=macro_score,
            macro_conf=macro_conf,
            news_score=news_score,
            news_conf=news_conf,
            cross_market_score=cross_market_score,
            cross_market_conf=cross_market_conf,
            session_score=session_score,
            session_conf=session_conf,
            london_range=london_range,
            calendar_covers_currency=calendar_covers_currency,
            upcoming_tier1_event=upcoming_tier1_event,
            meta_model=meta_model,
            account_state=account_state,
            open_count=open_count,
            exposure=exposure,
            recent_spread=recent_spread,
            reconciliation_ok=reconciliation_ok,
            allow_unverified_event_risk=allow_unverified_event_risk,
            auto_execute=auto_execute,
            now=now,
            economic_surprises=economic_surprises,
            challengers=challengers,
            usd_rates=usd_rates,
        )


@dataclass
class BrokerCycleContext:
    """Everything that used to be fetched once per user per cycle
    (evaluate_pair's own docstring explains why: identical for every pair
    in one broker's slice of the instrument list, so re-fetching per pair
    was pure waste) — now fetched once per BROKER KIND present in a user's
    instrument list instead of once globally, since a user can now have
    both OANDA and Alpaca instruments in the same cycle with separate
    accounts, balances, and open positions."""
    broker: Any
    execution_service: ExecutionService
    price_models: dict[tuple[str, str], object]
    account_state: object
    open_count: int
    exposure: dict[str, float]
    reconciliation_ok: bool
    usd_rates: dict[str, float]


async def _build_broker_cycle_context(
    broker_kind: BrokerKind, settings: Settings, engine, user_id: int, mode: str, instruments: list[str],
) -> BrokerCycleContext:
    if broker_kind == "alpaca":
        broker = AlpacaBroker(settings)
        # Alpaca's own paper endpoint IS the real (non-simulated) account —
        # there's no PaperBroker-equivalent wrapper for it (see
        # src/broker/alpaca.py's module docstring), so its orders_fills rows
        # are always tagged "demo" (a real, though not-real-money, broker
        # account) regardless of this user's OANDA-side paper/demo choice.
        exec_mode_for_broker = "demo"
    elif mode == "paper":
        broker = PaperBroker(settings, engine, user_id=user_id)
        exec_mode_for_broker = "paper"
    else:
        broker = OandaBroker(settings)
        exec_mode_for_broker = "demo" if mode == "demo" else "paper"

    execution_service = ExecutionService(broker, engine, execution_mode=exec_mode_for_broker, user_id=user_id)
    price_models = await build_price_models(broker, instruments)
    # Cross-currency USD conversion only ever means anything for forex
    # pairs (see _build_usd_conversion_rates' own "_" not in pair guard) —
    # skipped entirely for Alpaca, not just a no-op call.
    usd_rates = await _build_usd_conversion_rates(broker, instruments) if broker_kind == "oanda" else {}
    account_state = await broker.account_state()
    positions_raw = await broker.positions()
    open_count, exposure = compute_exposure(positions_raw, broker_kind, exec_mode_for_broker, usd_rates)
    reconciliation_ok = await execution_service.reconcile()
    return BrokerCycleContext(
        broker=broker, execution_service=execution_service, price_models=price_models,
        account_state=account_state, open_count=open_count, exposure=exposure,
        reconciliation_ok=reconciliation_ok, usd_rates=usd_rates,
    )


async def _run_once_for_user(
    user_ctx: UserTradingContext, engine, allow_unverified_event_risk: bool,
    economic_events: list[dict], news_events: list[dict], market_indicator_rows: list[dict],
    economic_surprises: list[dict], challengers: list[Challenger], meta_model,
) -> None:
    """One full decision cycle for exactly one user's own broker account(s)
    and instrument list — the "one shared engine, per-user data"
    architecture: same pipeline, same process, but every user trades
    independently against their own credentials and gets their own
    trade_intents/risk_decisions/orders_fills rows (see src/data/db.py's
    per-user table scoping). Global tables (predictions, market data) are
    computed once upstream and passed in, shared across every user watching
    the same instrument, rather than recomputed per user.

    A user's instrument list can span both OANDA (forex) and Alpaca
    (equities/crypto) — each broker kind present gets its own
    BrokerCycleContext (own account state, positions, exposure,
    ExecutionService), built once and reused for every instrument routed to
    that broker, mirroring the original single-broker optimization exactly,
    just keyed by broker kind now instead of assumed singular."""
    settings = user_ctx.settings
    mode = user_ctx.execution_mode
    auto_execute = user_ctx.auto_execute
    now = datetime.now(timezone.utc)

    # Per-instrument market-hours gate (replaces the old single global
    # should_run_cycle() bail-out — see cycle_cadence_ok's docstring for
    # why): each asset class has its own open/closed rule now, so a forex
    # weekend closure no longer skips this user's crypto instruments too,
    # and equities get their own NYSE-hours check instead of running (and
    # burning real Alpaca API calls) at 3am ET when the market is shut.
    skipped_closed = []
    open_instruments = []
    for pair in user_ctx.instrument_list:
        asset_class = asset_class_for(pair)
        if asset_class == "forex" and not is_forex_open(now):
            skipped_closed.append(pair)
        elif asset_class == "equity" and not is_nyse_open(now):
            skipped_closed.append(pair)
        else:  # crypto is always open; forex/equity already confirmed open above
            open_instruments.append(pair)
    if skipped_closed:
        logger.info("%s: %d instrument(s) skipped this cycle — market closed: %s",
                    user_ctx.email, len(skipped_closed), ", ".join(skipped_closed))

    instruments_by_broker: dict[BrokerKind, list[str]] = {}
    for pair in open_instruments:
        instruments_by_broker.setdefault(broker_kind_for(pair), []).append(pair)

    if "alpaca" in instruments_by_broker and not settings.alpaca_api_key:
        logger.warning(
            "%s: %d Alpaca-routed instrument(s) skipped this cycle — no Alpaca credentials configured",
            user_ctx.email, len(instruments_by_broker["alpaca"]),
        )
        del instruments_by_broker["alpaca"]

    if allow_unverified_event_risk:
        logger.warning(
            "DEV FLAG: --allow-unverified-event-risk is set. The event gate "
            "will NOT block trades even though no forward economic calendar "
            "is connected. Never use this flag against a real account."
        )
    if auto_execute:
        logger.warning(
            "%s: auto_execute is set. Risk-approved trades will be sent "
            "WITHOUT waiting for human authorization in the dashboard.",
            user_ctx.email,
        )

    broker_contexts: dict[BrokerKind, BrokerCycleContext] = {}
    try:
        for broker_kind, instruments in instruments_by_broker.items():
            broker_contexts[broker_kind] = await _build_broker_cycle_context(
                broker_kind, settings, engine, user_ctx.user_id, mode, instruments,
            )

        for broker_kind, instruments in instruments_by_broker.items():
            ctx = broker_contexts[broker_kind]
            for pair in instruments:
                await evaluate_pair(
                    broker=ctx.broker,
                    engine=engine,
                    user_id=user_ctx.user_id,
                    execution_service=ctx.execution_service,
                    execution_mode=mode,
                    price_models=ctx.price_models,
                    pair=pair,
                    economic_events=economic_events,
                    news_events=news_events,
                    market_indicator_rows=market_indicator_rows,
                    allow_unverified_event_risk=allow_unverified_event_risk,
                    auto_execute=auto_execute,
                    meta_model=meta_model,
                    now=now,
                    economic_surprises=economic_surprises,
                    challengers=challengers,
                    account_state=ctx.account_state,
                    open_count=ctx.open_count,
                    exposure=ctx.exposure,
                    reconciliation_ok=ctx.reconciliation_ok,
                    usd_rates=ctx.usd_rates,
                )
    finally:
        for ctx in broker_contexts.values():
            await ctx.broker.close()


async def run_once(
    default_mode: str, allow_unverified_event_risk: bool, default_auto_execute: bool = False,
    only_user_id: int | None = None,
) -> None:
    """Runs one decision cycle for every active, onboarded user (the
    scheduled-task path — AIForex_DemoTradingCycle), or for a single user
    when `only_user_id` is given (the dashboard's manual "run a cycle now"
    button, scoped to whoever is logged in). `default_mode`/
    `default_auto_execute` only matter as CLI-invocation compatibility —
    each user's actual mode/auto_execute comes from their own stored
    user_preferences (src/auth/service.py), not from this process-wide
    argument, since that's the whole point of per-user config."""
    settings = load_settings()
    engine = get_engine(settings.db_path)

    users = active_trading_users(engine)
    if only_user_id is not None:
        users = [u for u in users if u.user_id == only_user_id]
    if not users:
        logger.warning("No active, onboarded users to run a cycle for (only_user_id=%s).", only_user_id)
        return

    # Market/macro/news data and the champion/challenger roster are
    # instrument-agnostic-at-the-fetch-level and identical for every user
    # this cycle — computed once, not once per user, so N users watching
    # the same instrument don't redundantly refetch/recompute it.
    economic_events = _load_events(engine, economic_events_table)
    news_events = _load_events(engine, news_events_table)
    market_indicator_rows = _load_events(engine, market_indicators_table)
    economic_surprises = _load_events(engine, economic_surprises_table)
    challengers = active_challengers(engine)
    meta_model = load_deployed_meta_model(engine)
    if meta_model is not None:
        logger.info("Using deployed meta-model for confidence calibration")

    for user_ctx in users:
        logger.info("=== Running cycle for %s (mode=%s, %d instruments) ===",
                    user_ctx.email, user_ctx.execution_mode, len(user_ctx.instrument_list))
        try:
            await _run_once_for_user(
                user_ctx, engine, allow_unverified_event_risk,
                economic_events, news_events, market_indicator_rows,
                economic_surprises, challengers, meta_model,
            )
        except Exception:
            logger.exception("Cycle failed for %s; continuing with remaining users", user_ctx.email)


async def run_forever(mode: str, interval_seconds: int, allow_unverified_event_risk: bool, auto_execute: bool) -> None:
    while True:
        try:
            await run_once(mode, allow_unverified_event_risk, auto_execute)
        except Exception:
            logger.exception("Cycle failed; will retry next interval (fail closed — no orders assumed sent)")
        await asyncio.sleep(interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="AI Forex core decision loop")
    parser.add_argument("--mode", choices=["shadow", "paper", "demo"], default="shadow")
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit")
    parser.add_argument(
        "--interval", type=int, default=300,
        help="Seconds between cycles. Default 300s (5min) stays well ahead of "
        "the shortest (15m) horizon so signals don't sit stale between scans.",
    )
    parser.add_argument(
        "--allow-unverified-event-risk",
        action="store_true",
        help="DEV ONLY: bypass the event gate even though no forward economic "
        "calendar is connected. Never use against demo/live execution.",
    )
    parser.add_argument(
        "--auto-execute",
        action="store_true",
        help="Skip human authorization and send risk-approved orders immediately. "
        "Safe to use with --mode demo/paper (never real money — see module "
        "docstring) to accumulate outcome history for meta-model training. "
        "Every other risk gate still applies.",
    )
    args = parser.parse_args()

    if args.mode == "demo" and args.allow_unverified_event_risk:
        raise SystemExit(
            "Refusing to combine --mode demo with --allow-unverified-event-risk: "
            "the event gate now has a real calendar source, so there's no "
            "legitimate reason left to bypass it, even on demo."
        )

    if args.once:
        # Scheduled-task cadence gate (user-requested 2026-08-13): the task
        # trigger itself now fires every 2 minutes (tightened from a flat
        # 5) so session transitions/overlaps are never missed, but most
        # quiet-period ticks skip the expensive full cycle here rather than
        # paying for it every single time. Only gates this CLI/scheduled
        # path — the dashboard's manual "run cycle now" button calls
        # run_once() directly, bypassing main() entirely, so a human
        # explicitly asking for a scan is never throttled.
        run_cycle, reason = cycle_cadence_ok(datetime.now(timezone.utc))
        if not run_cycle:
            logger.info("Skipping this scheduled tick: %s", reason)
            return
        logger.info("Running scheduled cycle: %s", reason)
        asyncio.run(run_once(args.mode, args.allow_unverified_event_risk, args.auto_execute))
    else:
        asyncio.run(run_forever(args.mode, args.interval, args.allow_unverified_event_risk, args.auto_execute))


if __name__ == "__main__":
    main()

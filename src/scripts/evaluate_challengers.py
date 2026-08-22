"""Scores champion trade_intents and challenger shadow decisions against
real subsequent price data once each signal's horizon has actually elapsed
(Autonomous Upgrade Spec sec. 15: "periodically compare calibration,
expectancy, drawdown, hit rate, tail risk and stability... promote only
when improvement is persistent and not explained by a narrow sample").

Both champion and challengers are scored with the identical methodology —
same "did actual price move match the action's direction by horizon
expiry" check — so they're comparable on equal footing, not just the
champion's own logged win/loss (which only exists for the handful of
trades that were actually executed; this scores every signal, executed or
not, against real market data).

A challenger that reaches MIN_SAMPLES shadow evaluations and still hasn't
beaten the champion's own hit rate over the same period is buried in
strategy_graveyard with its reason — src/challengers/definitions.py's
active_challengers() then skips it on every future run_loop.py cycle.
Nothing here is ever auto-promoted; a challenger clearing the bar is only
ever logged as promotion-worthy for a human to review, same posture as the
meta-model's MIN_SAMPLES gate.

Run from the project root with the venv active:
    python -m src.scripts.evaluate_challengers
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from src.broker.alpaca import AlpacaBroker
from src.broker.oanda import OandaBroker
from src.broker.registry import broker_kind_for
from src.challengers.definitions import CHALLENGERS, active_challengers
from src.config import load_settings
from src.data.db import challenger_decisions as challenger_decisions_table
from src.data.db import get_engine
from src.data.db import signal_evaluations as signal_evaluations_table
from src.data.db import strategy_graveyard as strategy_graveyard_table
from src.data.db import trade_intents as trade_intents_table
from src.data.db import upsert_insert as insert
from src.run_loop import HORIZON_CONFIGS, horizon_duration

# Evaluated MIN_SAMPLES same as train_meta_model.py's own gate — enough that
# a run of luck one way or the other isn't mistaken for real skill, small
# enough that a genuinely useless challenger doesn't run in shadow forever
# before getting buried.
MIN_SAMPLES = 30

# Extra buffer past the horizon's own duration before we go looking for an
# expiry candle — covers weekend/holiday gaps and the odd missing bar
# without meaningfully changing what "at expiry" means.
EXPIRY_SEARCH_BUFFER = timedelta(hours=6)

_HORIZON_BY_LABEL = {cfg.label: cfg for cfg in HORIZON_CONFIGS}


def _naive_to_utc(row: dict) -> dict:
    return {
        k: (v.replace(tzinfo=timezone.utc) if isinstance(v, datetime) and v.tzinfo is None else v)
        for k, v in row.items()
    }


async def _expiry_price(
    brokers: dict[str, Any], instrument: str, horizon: str, signal_time: datetime, now: datetime,
) -> float | None:
    cfg = _HORIZON_BY_LABEL.get(horizon)
    if cfg is None:
        return None
    target = signal_time + horizon_duration(cfg)
    # OANDA rejects a 'to' timestamp in the future — a signal whose horizon
    # just elapsed can have target + buffer land past "now" by a few hours.
    to_time = min(target + EXPIRY_SEARCH_BUFFER, now)
    if to_time <= target:
        return None  # not enough elapsed real time yet to search a window at all
    # Real gap found live 2026-08-21: this used to hardcode a single
    # OandaBroker, which would raise (and crash this whole scheduled job,
    # not just skip one signal — no per-row try/except existed) the moment
    # any Alpaca-instrument trade_intent became eligible for scoring.
    broker = brokers.get(broker_kind_for(instrument))
    if broker is None:
        return None  # that broker isn't configured for this user — nothing to score against
    candles = await broker.get_candles_range(instrument, cfg.granularity, target, to_time)
    if not candles:
        return None
    return candles[0].close


async def _pending_champion_signals(engine, now: datetime) -> list[dict]:
    with engine.connect() as conn:
        already_evaluated = {
            row[0] for row in conn.execute(
                select(signal_evaluations_table.c.trade_intent_id)
                .where(signal_evaluations_table.c.source == "champion")
            )
        }
        rows = conn.execute(
            select(trade_intents_table).where(trade_intents_table.c.action != "NO_TRADE")
        ).mappings().all()
    pending = []
    for r in rows:
        if r["id"] in already_evaluated:
            continue
        row = _naive_to_utc(dict(r))
        cfg = _HORIZON_BY_LABEL.get(row["horizon"])
        if cfg is None or now < row["time"] + horizon_duration(cfg):
            continue  # horizon hasn't elapsed yet — not ready to score
        pending.append(row)
    return pending


async def _pending_challenger_signals(engine, now: datetime) -> list[dict]:
    with engine.connect() as conn:
        already_evaluated = {
            (row[0], row[1]) for row in conn.execute(
                select(signal_evaluations_table.c.source, signal_evaluations_table.c.trade_intent_id)
            )
        }
        rows = conn.execute(
            select(challenger_decisions_table).where(challenger_decisions_table.c.action != "NO_TRADE")
        ).mappings().all()
    pending = []
    for r in rows:
        key = (r["challenger_name"], r["trade_intent_id"])
        if key in already_evaluated:
            continue
        row = _naive_to_utc(dict(r))
        cfg = _HORIZON_BY_LABEL.get(row["horizon"])
        if cfg is None or now < row["computed_at"] + horizon_duration(cfg):
            continue
        pending.append(row)
    return pending


async def _score_and_store(engine, brokers: dict[str, Any], *, source: str, trade_intent_id: int, instrument: str,
                            horizon: str, action: str, signal_time: datetime, reference_price: float | None,
                            now: datetime, user_id: int | None) -> bool:
    if reference_price is None:
        return False  # older rows predating this column, or an unlinkable manual trade — can't score
    expiry_price = await _expiry_price(brokers, instrument, horizon, signal_time, now)
    if expiry_price is None:
        return False
    move_in_favor = (expiry_price - reference_price) if action == "BUY" else (reference_price - expiry_price)
    hit = move_in_favor > 0
    with engine.begin() as conn:
        stmt = insert(signal_evaluations_table).values(
            user_id=user_id, source=source, trade_intent_id=trade_intent_id, instrument=instrument, horizon=horizon,
            action=action, signal_time=signal_time, reference_price=reference_price,
            expiry_price=expiry_price, hit=hit, move_in_favor=move_in_favor, evaluated_at=now,
        )
        stmt = stmt.on_conflict_do_nothing(index_elements=["source", "trade_intent_id"])
        conn.execute(stmt)
    return True


def _hit_rate(engine, source: str) -> tuple[int, float | None]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(signal_evaluations_table.c.hit).where(signal_evaluations_table.c.source == source)
        ).all()
    n = len(rows)
    if n == 0:
        return 0, None
    return n, sum(1 for r in rows if r[0]) / n


def _review_for_graveyard(engine, now: datetime) -> None:
    champion_n, champion_hit_rate = _hit_rate(engine, "champion")
    for challenger in active_challengers(engine):
        n, hit_rate = _hit_rate(engine, challenger.name)
        if n < MIN_SAMPLES or champion_n < MIN_SAMPLES or hit_rate is None or champion_hit_rate is None:
            print(f"{challenger.name}: {n} shadow samples (need {MIN_SAMPLES}; champion has {champion_n}) — not enough evidence yet")
            continue
        if hit_rate <= champion_hit_rate:
            reason = (
                f"after {n} shadow evaluations, hit_rate={hit_rate:.1%} did not beat "
                f"champion's {champion_hit_rate:.1%} over the same period"
            )
            with engine.begin() as conn:
                stmt = insert(strategy_graveyard_table).values(
                    strategy_name=challenger.name, reason=reason, sample_size=n,
                    challenger_hit_rate=hit_rate, champion_hit_rate=champion_hit_rate, buried_at=now,
                )
                stmt = stmt.on_conflict_do_nothing(index_elements=["strategy_name"])
                conn.execute(stmt)
            print(f"{challenger.name}: BURIED — {reason}")
        else:
            print(
                f"{challenger.name}: {n} samples, hit_rate={hit_rate:.1%} vs champion {champion_hit_rate:.1%} "
                "— outperforming so far; NOT auto-promoted, needs human review before any weight change"
            )


async def main() -> None:
    settings = load_settings()
    engine = get_engine(settings.db_path)
    brokers: dict[str, Any] = {"oanda": OandaBroker(settings)}
    if settings.alpaca_api_key:
        brokers["alpaca"] = AlpacaBroker(settings)
    now = datetime.now(timezone.utc)

    try:
        champion_pending = await _pending_champion_signals(engine, now)
        scored = 0
        for row in champion_pending:
            ok = await _score_and_store(
                engine, brokers, source="champion", trade_intent_id=row["id"], instrument=row["instrument"],
                horizon=row["horizon"], action=row["action"], signal_time=row["time"],
                reference_price=row["reference_price"], now=now, user_id=row.get("user_id"),
            )
            scored += int(ok)
        print(f"Champion: scored {scored}/{len(champion_pending)} elapsed signals")

        challenger_pending = await _pending_challenger_signals(engine, now)
        scored = 0
        for row in challenger_pending:
            ok = await _score_and_store(
                engine, brokers, source=row["challenger_name"], trade_intent_id=row["trade_intent_id"],
                instrument=row["instrument"], horizon=row["horizon"], action=row["action"],
                signal_time=row["computed_at"], reference_price=row["reference_price"], now=now,
                user_id=row.get("user_id"),
            )
            scored += int(ok)
        print(f"Challengers: scored {scored}/{len(challenger_pending)} elapsed signals")

        _review_for_graveyard(engine, now)
    finally:
        for broker in brokers.values():
            await broker.close()


if __name__ == "__main__":
    asyncio.run(main())

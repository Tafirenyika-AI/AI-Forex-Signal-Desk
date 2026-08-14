"""Matches FRED actuals against Forex Factory consensus, measures the real
price reaction, and stores the result (Autonomous Upgrade Spec sec. 7).

Run from the project root with the venv active:
    python -m src.scripts.compute_economic_surprises
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from src.broker.oanda import OandaBroker
from src.config import load_settings
from src.data.db import economic_events, economic_surprises, get_engine
from src.models.market_reaction import compute_price_reaction
from src.models.surprise_engine import detect_reaction_mismatch, match_surprises


def _naive_to_utc(row: dict) -> dict:
    return {
        k: (v.replace(tzinfo=timezone.utc) if isinstance(v, datetime) and v.tzinfo is None else v)
        for k, v in row.items()
    }


async def main() -> None:
    settings = load_settings()
    engine = get_engine(settings.db_path)

    with engine.connect() as conn:
        fred_rows = [_naive_to_utc(dict(r)) for r in conn.execute(
            select(economic_events).where(economic_events.c.source == "FRED")
        ).mappings().all()]
        ff_rows = [_naive_to_utc(dict(r)) for r in conn.execute(
            select(economic_events).where(economic_events.c.source == "ForexFactory")
        ).mappings().all()]

    surprises = match_surprises(fred_rows, ff_rows)
    if not surprises:
        print("No FRED x Forex Factory matches found yet.")
        return

    now = datetime.now(timezone.utc)
    stored = 0
    async with OandaBroker(settings) as broker:
        with engine.begin() as conn:
            for s in surprises:
                # Only measure reaction for events far enough in the past
                # that the full 60-min post-event window has actually
                # happened — no point fetching candles for the future.
                reaction = None
                if s.ff_event_time < now:
                    try:
                        reaction = await compute_price_reaction(broker, s.currency, s.ff_event_time)
                    except Exception:  # noqa: BLE001 — a candle-fetch failure shouldn't block the rest
                        reaction = None
                mismatch = detect_reaction_mismatch(s, reaction) if reaction is not None else None

                stmt = insert(economic_surprises).values(
                    currency=s.currency, fred_event_name=s.fred_event_name, ff_event_name=s.ff_event_name,
                    actual=s.actual, comparable_actual=s.comparable_actual, consensus=s.consensus,
                    previous=s.previous, surprise_vs_consensus=s.surprise_vs_consensus,
                    surprise_vs_previous=s.surprise_vs_previous, fred_event_time=s.fred_event_time,
                    ff_event_time=s.ff_event_time, ff_importance=s.ff_importance,
                    price_reaction_60min=reaction, mismatch=mismatch, computed_at=now,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["currency", "fred_event_name", "ff_event_time"],
                    set_={"price_reaction_60min": reaction, "mismatch": mismatch, "computed_at": now},
                )
                conn.execute(stmt)
                stored += 1

                reaction_str = f"{reaction:+.3%}" if reaction is not None else "n/a"
                print(f"{s.currency} {s.ff_event_name}: surprise={s.surprise_vs_consensus:+.3f} "
                      f"reaction={reaction_str} mismatch={mismatch}")

    print(f"\nStored/updated {stored} economic surprise record(s).")


if __name__ == "__main__":
    asyncio.run(main())

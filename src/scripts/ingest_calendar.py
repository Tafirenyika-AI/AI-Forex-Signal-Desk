"""Pulls the free Forex Factory forward-looking calendar (schedule + impact
+ consensus forecast) into economic_events. This is what unblocks the risk
governor's event-timing gate (src/risk/governor.py) — see
src/macro/forex_factory.py's docstring for what this feed can and can't do.

Run this periodically (e.g. every 30-60 minutes is plenty — the calendar
doesn't change intraday) as its own job, NOT once per decision cycle: the
feed has no API key and a shared, global rate limit of ~2 requests/5min
across everyone using this exact URL.

Run from the project root with the venv active:
    python -m src.scripts.ingest_calendar
"""
from __future__ import annotations

import asyncio

from sqlalchemy.dialects.sqlite import insert

from src.config import load_settings
from src.data.db import economic_events, get_engine
from src.macro.forex_factory import ForexFactoryCalendar


async def main() -> None:
    settings = load_settings()
    engine = get_engine(settings.db_path)

    async with ForexFactoryCalendar() as calendar:
        rows = await calendar.fetch_upcoming_events()
        if not rows:
            print("No events returned (feed may be rate-limited or unreachable right now).")
            return

        with engine.begin() as conn:
            stmt = insert(economic_events)
            stmt = stmt.on_conflict_do_update(
                index_elements=["event_time", "currency", "event_name", "source"],
                set_={
                    "consensus": stmt.excluded.consensus,
                    "previous": stmt.excluded.previous,
                    "importance": stmt.excluded.importance,
                    "ingested_at": stmt.excluded.ingested_at,
                },
            )
            conn.execute(stmt, rows)

        print(f"Stored/updated {len(rows)} forward-looking events from Forex Factory.")
        high_impact = [r for r in rows if r["importance"] == "high"]
        print(f"  {len(high_impact)} are High impact:")
        for r in sorted(high_impact, key=lambda r: r["event_time"])[:15]:
            print(f"    {r['event_time']:%Y-%m-%d %H:%M UTC} {r['currency']} {r['event_name']} "
                  f"(consensus={r['consensus']}, previous={r['previous']})")


if __name__ == "__main__":
    asyncio.run(main())

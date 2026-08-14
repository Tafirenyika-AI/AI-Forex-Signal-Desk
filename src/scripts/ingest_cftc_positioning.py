"""Pulls weekly CFTC Commitments of Traders data for all 6 currencies
(Autonomous Upgrade Spec sec. 6, 10).

COT is published weekly (Fridays) — run this daily is plenty; it's
idempotent and cheap either way.

Run from the project root with the venv active:
    python -m src.scripts.ingest_cftc_positioning
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from src.config import load_settings
from src.data.db import cftc_positioning as cftc_positioning_table
from src.data.db import get_engine
from src.data.db import upsert_insert as insert
from src.positioning.cftc import CONTRACT_NAMES, fetch_positioning_history


async def main() -> None:
    settings = load_settings()
    engine = get_engine(settings.db_path)
    now = datetime.now(timezone.utc)

    total = 0
    for currency in CONTRACT_NAMES:
        history = await fetch_positioning_history(currency, weeks=60)
        if not history:
            print(f"{currency}: no data returned")
            continue

        with engine.begin() as conn:
            for row in history:
                stmt = insert(cftc_positioning_table).values(
                    currency=row["currency"], report_date=row["report_date"],
                    noncomm_long=row["noncomm_long"], noncomm_short=row["noncomm_short"],
                    net_position=row["net_position"], open_interest=row["open_interest"],
                    ingested_at=now,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["currency", "report_date"],
                    set_={"noncomm_long": row["noncomm_long"], "noncomm_short": row["noncomm_short"],
                          "net_position": row["net_position"], "open_interest": row["open_interest"]},
                )
                conn.execute(stmt)
        total += len(history)
        latest = sorted(history, key=lambda r: r["report_date"])[-1]
        print(f"{currency}: {len(history)} weeks, latest={latest['report_date'].date()} "
              f"net_position={latest['net_position']:.0f}")

    print(f"\nStored/updated {total} CFTC positioning rows.")


if __name__ == "__main__":
    asyncio.run(main())

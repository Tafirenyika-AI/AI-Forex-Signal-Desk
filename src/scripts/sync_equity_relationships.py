"""Equity V2 Phase 7 scheduled entrypoint — enriches every equity_entities
row that already has a CIK (written by Phase 4's SEC EDGAR sync) with its
real SIC industry/sector/sector-ETF/exchange, via SEC's own submissions
endpoint. Read-only against SEC's public API; the only write is an UPDATE
of equity_entities' own enrichment columns — no new rows, no broker call.

Slow-changing data (a company's SIC classification and listing exchange
don't change often) — daily is plenty, same cadence as Phase 4's own SEC
sync it depends on.

Run from the project root with the venv active:
    python -m src.scripts.sync_equity_relationships
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from src.config import load_settings
from src.data.db import equity_entities as equity_entities_table
from src.data.db import get_engine
from src.equity.relationships import SecRelationshipClient

SEC_EDGAR_CONTACT_EMAIL = "tafishoniwa@gmail.com"


def _tickers_with_cik(engine) -> list[tuple[str, str]]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(equity_entities_table.c.ticker, equity_entities_table.c.cik)
            .where(equity_entities_table.c.cik.is_not(None))
        ).fetchall()
    return [(r.ticker, r.cik) for r in rows]


async def main() -> None:
    settings = load_settings()
    engine = get_engine(settings.db_path)

    targets = _tickers_with_cik(engine)
    if not targets:
        print("No equity_entities rows with a CIK yet — run sync_sec_edgar first.")
        return

    async with SecRelationshipClient(contact_email=SEC_EDGAR_CONTACT_EMAIL) as client:
        for ticker, cik in targets:
            try:
                row = await client.enrichment_row(ticker, cik)
                with engine.begin() as conn:
                    conn.execute(
                        equity_entities_table.update()
                        .where(equity_entities_table.c.ticker == ticker)
                        .values(
                            sic_code=row["sic_code"], industry=row["industry"], sector=row["sector"],
                            sector_etf=row["sector_etf"], exchange=row["exchange"], updated_at=row["updated_at"],
                        )
                    )
                print(f"  {ticker}: SIC {row['sic_code']} ({row['industry']}) -> "
                      f"sector={row['sector']} etf={row['sector_etf']} exchange={row['exchange']}")
            except Exception as exc:  # noqa: BLE001 — one ticker's failure shouldn't stop the rest
                print(f"  {ticker}: FAILED — {exc!r}")


if __name__ == "__main__":
    asyncio.run(main())

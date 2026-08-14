"""Pulls currency-targeted news from GDELT into news_events (source="gdelt").

Fetches 6 currency queries, paced 15s apart (see src/news/gdelt.py's
MIN_REQUEST_INTERVAL_SECONDS docstring — GDELT's documented 5s limit was
verified live to 429 well beyond that spacing, so this paces more
conservatively than the stated number) — a full run takes ~90 seconds. Run
this every 15-30 minutes, not "every few minutes" as spec sec. 20 suggests
for news in general — GDELT is free and unauthenticated, and pacing
conservatively is the same respectful-citizen posture already taken with
the Forex Factory calendar feed.

Run from the project root with the venv active:
    python -m src.scripts.ingest_gdelt_news
"""
from __future__ import annotations

import asyncio
import io
import sys
from datetime import datetime, timezone

# Windows' console defaults to cp1252, which can't encode a lot of
# characters that show up in real international headlines (smart quotes,
# em-dashes, accented names) — verified live 2026-08-13 when a genuine
# headline crashed the summary print after the DB write had already
# succeeded. Re-wrapping stdout as UTF-8 is the general fix, not a
# per-character workaround.
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from src.config import load_settings
from src.data.db import get_engine
from src.data.db import news_events as news_events_table
from src.data.db import upsert_insert as insert
from src.news.gdelt import GDELTClient, title_sentiment

MAX_DISTINCT_DOMAIN_BONUS = 0.25
DOMAIN_BONUS_PER_EXTRA_DOMAIN = 0.08
BASE_CONFIDENCE = 0.35
TITLE_KEYWORD_BONUS = 0.25


def _confidence(row: dict) -> float:
    conf = BASE_CONFIDENCE
    if row.get("_query_keyword_in_title"):
        conf += TITLE_KEYWORD_BONUS
    extra_domains = max(0, row.get("_distinct_domain_count", 1) - 1)
    conf += min(MAX_DISTINCT_DOMAIN_BONUS, extra_domains * DOMAIN_BONUS_PER_EXTRA_DOMAIN)
    return min(1.0, conf)


def _parse_gdelt_time(value: str) -> datetime:
    # GDELT seendate format: "20260813T144500Z"
    return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


async def main() -> None:
    settings = load_settings()
    engine = get_engine(settings.db_path)
    now = datetime.now(timezone.utc)

    async with GDELTClient() as client:
        merged = await client.fetch_all_currencies()

    if not merged:
        print("No articles returned (GDELT may be rate-limiting or unreachable right now).")
        return

    rows = []
    for item in merged:
        try:
            publish_time = _parse_gdelt_time(item["seendate"])
        except (KeyError, ValueError):
            continue
        currencies = sorted(item.get("_currencies", set()))
        if not currencies:
            continue
        rows.append(
            {
                "publish_time": publish_time,
                "ingest_time": now,
                "source": "gdelt",
                "headline": item.get("title", "")[:500],
                "url": item.get("url"),
                "currencies": ",".join(currencies),
                "event_type": None,
                "sentiment_score": title_sentiment(item.get("title", "")),
                "novelty_score": None,
                "confidence": _confidence(item),
            }
        )

    with engine.begin() as conn:
        for row in rows:
            stmt = insert(news_events_table).values(**row)
            stmt = stmt.on_conflict_do_nothing(index_elements=["url"])
            conn.execute(stmt)

    print(f"Stored {len(rows)} GDELT news_events rows (duplicates by URL skipped).")
    multi_currency = [r for r in rows if "," in r["currencies"]]
    print(f"  {len(multi_currency)} matched more than one currency query.")
    for row in sorted(rows, key=lambda r: r["confidence"], reverse=True)[:5]:
        print(f"  conf={row['confidence']:.2f} sent={row['sentiment_score']:+.2f} "
              f"[{row['currencies']}] {row['headline'][:80]}")


if __name__ == "__main__":
    asyncio.run(main())

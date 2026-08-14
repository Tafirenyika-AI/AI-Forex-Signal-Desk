"""Archives Federal Reserve statements, diffs each new one against the
prior statement of the same type, scores tone, and extracts dissenting
members (Autonomous Upgrade Spec sec. 9).

Only the Fed is covered — its RSS feed is a real, structured, machine-
readable source (verified live). ECB/BoE/BoJ/BoC/RBA are a natural
extension once/if equivalent structured feeds are found for them.

Run from the project root with the venv active:
    python -m src.scripts.ingest_central_bank
"""
from __future__ import annotations

import asyncio
import io
import json
import sys
from datetime import datetime, timezone

from sqlalchemy import select

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from src.central_bank.diff_engine import (
    classify_policy_regime,
    diff_statements,
    extract_dissenting_members,
    hawkish_dovish_score,
)
from src.central_bank.fed_source import fetch_recent_statements
from src.config import load_settings
from src.data.db import central_bank_statements, get_engine
from src.data.db import upsert_insert as insert


def _statement_type(title: str) -> str:
    if "FOMC statement" in title:
        return "FOMC statement"
    if "Minutes" in title:
        return "Minutes"
    return "Other"


async def main() -> None:
    settings = load_settings()
    engine = get_engine(settings.db_path)

    items = await fetch_recent_statements(max_items=15)
    if not items:
        print("No statements fetched (feed may be unreachable right now).")
        return
    # Oldest first: diffing needs each item's "prior statement of the same
    # type" to already be stored — processing newest-first meant the very
    # diff comparisons that matter (latest vs previous) always found
    # nothing to compare against, since the previous one hadn't been
    # inserted yet within the same run.
    items = sorted(items, key=lambda i: i["published_at"])

    now = datetime.now(timezone.utc)
    stored = 0
    with engine.begin() as conn:
        for item in items:
            if not item["full_text"]:
                continue
            stmt_type = _statement_type(item["title"])

            # Find the most recent already-archived statement of the SAME
            # type, older than this one, to diff against.
            prior = conn.execute(
                select(central_bank_statements)
                .where(central_bank_statements.c.statement_type == stmt_type)
                .where(central_bank_statements.c.published_at < item["published_at"])
                .order_by(central_bank_statements.c.published_at.desc())
                .limit(1)
            ).mappings().first()

            tone = hawkish_dovish_score(item["full_text"])
            if prior:
                diff = diff_statements(prior["full_text"], item["full_text"])
                tone_shift, change_ratio = diff.tone_shift, diff.change_ratio
                regime = classify_policy_regime(diff.new_tone, diff.tone_shift)
            else:
                tone_shift, change_ratio, regime = None, None, classify_policy_regime(tone, 0.0)

            dissenters = extract_dissenting_members(item["full_text"])

            insert_stmt = insert(central_bank_statements).values(
                central_bank="Federal Reserve", statement_type=stmt_type,
                title=item["title"], url=item["url"], published_at=item["published_at"],
                full_text=item["full_text"], hawkish_dovish_tone=tone,
                tone_shift_vs_previous=tone_shift, change_ratio_vs_previous=change_ratio,
                policy_regime=regime, dissenting_members_json=json.dumps(dissenters),
                ingested_at=now,
            )
            insert_stmt = insert_stmt.on_conflict_do_nothing(index_elements=["url"])
            result = conn.execute(insert_stmt)
            if result.rowcount:
                stored += 1
                print(f"{item['published_at'].date()} [{stmt_type}] {item['title'][:70]}")
                print(f"  tone={tone:+.2f} shift={tone_shift} change_ratio={change_ratio} regime={regime}")
                if dissenters:
                    print(f"  dissenters: {', '.join(dissenters)}")

    print(f"\nStored {stored} new central bank statement(s).")


if __name__ == "__main__":
    asyncio.run(main())

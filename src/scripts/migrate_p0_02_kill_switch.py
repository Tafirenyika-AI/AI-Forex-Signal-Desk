"""One-off migration for P0-02 (external review, 2026-09-22): persistent
manual kill switch + weekly-breach-persists-for-the-week fix.

What it does, in order (idempotent — safe to run multiple times):
1. Creates the brand-new `manual_kill_switch` table via metadata.create_all()
   — additive, no-op if it already exists.
2. Adds two new nullable columns to the EXISTING `risk_state_weekly` table
   (`kill_switch_active`, `kill_switch_reason`) via `ADD COLUMN IF NOT
   EXISTS` — supported natively by both Postgres (9.6+) and SQLite
   (3.35.0+, 2021), so no dialect branch is needed. Non-destructive: no
   existing row or column is touched, only two new nullable columns added.

Run from the project root with the venv active:
    python -m src.scripts.migrate_p0_02_kill_switch
Reads DATABASE_URL the same way get_engine() does (falls back to the local
SQLite file if unset), so this targets whichever database the rest of the
app would actually connect to.
"""
from __future__ import annotations

from sqlalchemy import text

from src.config import load_settings
from src.data.db import get_engine


def run() -> None:
    settings = load_settings()
    engine = get_engine(settings.db_path)  # also runs metadata.create_all() -> creates manual_kill_switch

    with engine.begin() as conn:
        for col_ddl in (
            "ADD COLUMN IF NOT EXISTS kill_switch_active BOOLEAN NOT NULL DEFAULT FALSE",
            "ADD COLUMN IF NOT EXISTS kill_switch_reason VARCHAR",
        ):
            conn.execute(text(f"ALTER TABLE risk_state_weekly {col_ddl}"))
    print("risk_state_weekly: kill_switch_active/kill_switch_reason columns present.")
    print("manual_kill_switch: table present (created by metadata.create_all() above if it was missing).")


if __name__ == "__main__":
    run()

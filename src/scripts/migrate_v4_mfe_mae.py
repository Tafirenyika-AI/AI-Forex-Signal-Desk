"""One-off migration for AI Trading Desk V4 Priority 2 (docs/V4_ARCHITECTURE.md
Section 14 gap): adds `mfe_usd`/`mae_usd` nullable columns to the EXISTING
`trade_outcomes` table.

Idempotent — safe to run multiple times. `ADD COLUMN IF NOT EXISTS` is
supported natively by both Postgres (9.6+) and SQLite (3.35.0+), same idiom
already used by src/scripts/migrate_p0_02_kill_switch.py. Non-destructive: no
existing row or column is touched, only two new nullable columns added.

Run from the project root with the venv active:
    python -m src.scripts.migrate_v4_mfe_mae
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
    engine = get_engine(settings.db_path)  # also runs metadata.create_all()

    with engine.begin() as conn:
        for col_ddl in (
            "ADD COLUMN IF NOT EXISTS mfe_usd DOUBLE PRECISION",
            "ADD COLUMN IF NOT EXISTS mae_usd DOUBLE PRECISION",
        ):
            conn.execute(text(f"ALTER TABLE trade_outcomes {col_ddl}"))
    print("trade_outcomes: mfe_usd/mae_usd columns present.")


if __name__ == "__main__":
    run()

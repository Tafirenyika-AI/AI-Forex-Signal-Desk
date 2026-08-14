"""One-off migration: single-user -> multi-user. Run exactly once.

What it does, in order:
1. Backs up data/forex.db (unconditionally, before touching anything).
2. Creates every brand-new table (users, invitations, sessions,
   user_oanda_accounts, user_preferences) via metadata.create_all() — safe,
   additive, no-op for tables that already exist.
3. Adds a nullable `user_id` column to every table that only needed a new
   column (ALTER TABLE ... ADD COLUMN — SQLite supports this directly).
4. Rebuilds the 4 tables whose *constraints* changed, not just their
   columns (risk_state, risk_state_weekly, paper_positions,
   paper_account_state) using the standard SQLite pattern: rename the old
   table out of the way, let SQLAlchemy create the new-schema table fresh
   from its real Table() definition in src/data/db.py (so this script can
   never drift from that source of truth), copy every row across with the
   admin's user_id attached, drop the renamed-away original.
5. Creates the admin user (email/username/password), encrypts and stores
   their existing OANDA credentials (read from the current .env — so their
   already-running demo trading keeps working with zero interruption), and
   backfills every per-user table's new user_id column to the admin's id.

Idempotent: if `users` already has rows, refuses to run again rather than
risk double-migrating.

Run from the project root with the venv active. The admin password is
never hardcoded here — pass it via the ADMIN_PASSWORD environment variable
for this one invocation, e.g. (PowerShell):
    $env:ADMIN_PASSWORD = "..."; python -m src.scripts.migrate_multiuser
or it will prompt interactively if that env var isn't set.
"""
from __future__ import annotations

import getpass
import json
import shutil
from datetime import datetime, timezone

from sqlalchemy import inspect, select, text

from src.auth.crypto import encrypt_secret, hash_password
from src.config import load_settings
from src.data import db
from src.data.db import get_engine
from src.data.db import upsert_insert as insert

ADMIN_EMAIL = "shoniwatafirenyika@gmail.com"
ADMIN_USERNAME = "shoniwatafirenyika@gmail.com"  # same value serves as either login field

# Preserves exactly what's live today rather than silently expanding scope
# mid-migration — the admin can opt into the full instrument universe
# later via preferences once Phase 4 builds that out.
CURRENT_PAIRS = ["EUR_USD", "GBP_USD", "USD_JPY", "USD_CAD", "AUD_USD"]

# Tables that only need a new nullable user_id column.
SIMPLE_ADD_COLUMN_TABLES = [
    "trade_intents", "risk_decisions", "orders_fills", "authorizations",
    "trade_outcomes", "trade_attribution", "trade_analogs",
    "challenger_decisions", "signal_evaluations", "promotion_gate_snapshots",
    "paper_order_ids_seen",
]

# Tables whose constraints changed — handled via rename/recreate/copy/drop.
REBUILD_TABLES = ["risk_state", "risk_state_weekly", "paper_positions", "paper_account_state"]


def _already_migrated(engine) -> bool:
    inspector = inspect(engine)
    if "users" not in inspector.get_table_names():
        return False
    with engine.connect() as conn:
        count = conn.execute(select(db.users.c.id)).first()
    return count is not None


def _backup(db_path) -> None:
    backup_path = db_path.parent / f"{db_path.name}.pre_multiuser_backup"
    if backup_path.exists():
        print(f"Backup already exists at {backup_path}, leaving it as-is (not overwriting).")
        return
    shutil.copy2(db_path, backup_path)
    print(f"Backed up {db_path} -> {backup_path}")


def _add_user_id_columns(engine) -> None:
    with engine.begin() as conn:
        for table_name in SIMPLE_ADD_COLUMN_TABLES:
            try:
                conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN user_id INTEGER"))
                print(f"  added user_id column to {table_name}")
            except Exception as exc:  # noqa: BLE001
                if "duplicate column" in str(exc).lower():
                    print(f"  {table_name}.user_id already exists, skipping")
                else:
                    raise


def _rebuild_constrained_tables(engine, admin_id: int) -> None:
    """Renames each old-schema table out of the way, creates the new-schema
    version fresh from its real Table() object (so this script can never
    drift from src/data/db.py's actual definitions), then copies data
    across with a single raw `INSERT INTO ... SELECT` per table — deliberately
    NOT round-tripping rows through Python/SQLAlchemy's typed `.values()`,
    since SQLite stores DateTime columns as plain TEXT and handing that TEXT
    back to SQLAlchemy's DateTime type as a bind parameter raises
    `SQLite DateTime type only accepts Python datetime and date objects`.
    A same-database, same-column-type `INSERT ... SELECT` sidesteps that
    entirely — the TEXT value is copied verbatim, exactly as intended."""
    legacy_suffix = "_legacy_premultiuser"
    with engine.begin() as conn:
        for table_name in REBUILD_TABLES:
            conn.execute(text(f"ALTER TABLE {table_name} RENAME TO {table_name}{legacy_suffix}"))
            print(f"  renamed {table_name} -> {table_name}{legacy_suffix}")

    # Table.create() reads the real Table() object from src/data/db.py, so
    # this script can never drift from the actual schema definitions.
    for table_name in REBUILD_TABLES:
        db.metadata.tables[table_name].create(engine)
        print(f"  created new-schema {table_name}")

    with engine.begin() as conn:
        conn.execute(text(
            f"INSERT INTO risk_state (user_id, day, day_start_balance, kill_switch_active, "
            f"kill_switch_reason, updated_at) SELECT :uid, day, day_start_balance, "
            f"kill_switch_active, kill_switch_reason, updated_at FROM risk_state{legacy_suffix}"
        ), {"uid": admin_id})

        conn.execute(text(
            f"INSERT INTO risk_state_weekly (user_id, iso_week, week_start_balance, updated_at) "
            f"SELECT :uid, iso_week, week_start_balance, updated_at FROM risk_state_weekly{legacy_suffix}"
        ), {"uid": admin_id})

        conn.execute(text(
            f"INSERT INTO paper_positions (user_id, instrument, units, avg_price, updated_at) "
            f"SELECT :uid, instrument, units, avg_price, updated_at FROM paper_positions{legacy_suffix}"
        ), {"uid": admin_id})

        conn.execute(text(
            f"INSERT INTO paper_account_state (user_id, starting_balance, realized_pl, updated_at) "
            f"SELECT :uid, starting_balance, realized_pl, updated_at FROM paper_account_state{legacy_suffix}"
        ), {"uid": admin_id})

        for table_name in REBUILD_TABLES:
            conn.execute(text(f"DROP TABLE {table_name}{legacy_suffix}"))
            print(f"  dropped {table_name}{legacy_suffix}")


def _backfill_admin_user_id(engine, admin_id: int) -> None:
    with engine.begin() as conn:
        for table_name in SIMPLE_ADD_COLUMN_TABLES:
            result = conn.execute(text(f"UPDATE {table_name} SET user_id = :uid WHERE user_id IS NULL"),
                                   {"uid": admin_id})
            print(f"  backfilled {result.rowcount} row(s) in {table_name}")


def main() -> None:
    settings = load_settings()
    engine = get_engine(settings.db_path)

    if _already_migrated(engine):
        print("Already migrated (users table has rows) — refusing to run again. Nothing done.")
        return

    print(f"Migrating {settings.db_path} to multi-user schema...")
    _backup(settings.db_path)

    import os
    password = os.environ.get("ADMIN_PASSWORD") or getpass.getpass(f"Set initial password for {ADMIN_EMAIL}: ")
    if not password:
        raise SystemExit("No admin password provided — aborting migration.")

    db.metadata.create_all(engine)  # creates the 5 brand-new tables; no-op for tables that already exist
    print("Created new identity tables (users/invitations/sessions/user_oanda_accounts/user_preferences).")

    print("Adding user_id columns to per-user tables...")
    _add_user_id_columns(engine)

    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        result = conn.execute(insert(db.users).values(
            email=ADMIN_EMAIL, username=ADMIN_USERNAME, password_hash=hash_password(password),
            is_admin=True, status="active", created_at=now, updated_at=now,
        ))
        admin_id = result.inserted_primary_key[0]
    print(f"Created admin user id={admin_id} ({ADMIN_EMAIL}).")

    print("Rebuilding constraint-changed tables (risk_state, risk_state_weekly, paper_positions, paper_account_state)...")
    _rebuild_constrained_tables(engine, admin_id)

    print("Backfilling admin's user_id onto pre-existing rows...")
    _backfill_admin_user_id(engine, admin_id)

    with engine.begin() as conn:
        conn.execute(insert(db.user_oanda_accounts).values(
            user_id=admin_id,
            encrypted_api_token=encrypt_secret(settings.oanda_api_token),
            oanda_account_id=settings.oanda_account_id,
            oanda_environment=settings.oanda_environment,
            created_at=now, updated_at=now,
        ))
        conn.execute(insert(db.user_preferences).values(
            user_id=admin_id,
            instrument_list_json=json.dumps(CURRENT_PAIRS),
            execution_mode_default="demo",
            auto_execute=True,  # matches the already-running AIForex_DemoTradingCycle --auto-execute flag
            onboarding_complete=True,
            created_at=now, updated_at=now,
        ))
    print("Seeded admin's OANDA credentials (from current .env) and preferences (current 5-pair setup preserved).")

    print("\nMigration complete.")


if __name__ == "__main__":
    main()

"""One-off migration: local SQLite -> shared Postgres (Neon), for real
local/cloud parity (2026-08-14). Run exactly once; safely re-runnable
against an empty target (each table load is wrapped so partial failures
don't silently corrupt row counts — but it is NOT idempotent against a
partially-populated target, so don't re-run after a partial success
without clearing the target tables first).

Reads the local SQLite file directly (read-only — never modifies it) and
writes into whatever DATABASE_URL points at. Preserves every row's exact
primary-key id (other tables reference these informally — no DB-level
ForeignKey constraints exist in this schema, confirmed via grep, but the
application code relies on the values matching), then resets each table's
Postgres auto-increment sequence to continue after the highest migrated id
so future inserts don't collide.

SQLite drops timezone info on round-trip even for columns declared
DateTime(timezone=True) (every value in this system is conceptually UTC —
written via datetime.now(timezone.utc) throughout — SQLite just doesn't
store that fact). Every DateTime-typed column's naive values are given
explicit UTC tzinfo before insert, so Postgres's real TIMESTAMPTZ columns
store the correct instant rather than reinterpreting a naive value in
whatever timezone the connection defaults to.

Run from the project root with the venv active, with DATABASE_URL already
pointing at the target Postgres database:
    python -m src.scripts.migrate_to_postgres
"""
from __future__ import annotations

import os
from datetime import timezone
from pathlib import Path

from sqlalchemy import DateTime, Integer, create_engine, select, text

from src.data.db import metadata

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
LOCAL_DB_PATH = ROOT_DIR / "data" / "forex.db"
BATCH_SIZE = 2000


def _normalize_row(row: dict, table) -> dict:
    row = dict(row)
    for col in table.columns:
        if isinstance(col.type, DateTime) and row.get(col.name) is not None:
            value = row[col.name]
            if value.tzinfo is None:
                row[col.name] = value.replace(tzinfo=timezone.utc)
    return row


def main() -> None:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("DATABASE_URL is not set — point it at the target Postgres database first.")

    # SOURCE_DATABASE_URL supports Postgres-to-Postgres migration (e.g.
    # switching Neon region after discovering the first one was too far
    # away, 2026-08-14 — the running system had already written new rows
    # there since the original SQLite migration, so that Postgres instance,
    # not the now-stale local file, is the real source of truth by then).
    # Falls back to the local SQLite file when unset, as originally built.
    source_url = os.environ.get("SOURCE_DATABASE_URL", "").strip()
    if source_url:
        source_engine = create_engine(source_url.replace("postgresql://", "postgresql+psycopg://", 1), future=True)
    else:
        if not LOCAL_DB_PATH.exists():
            raise SystemExit(f"Local database not found at {LOCAL_DB_PATH}")
        source_engine = create_engine(f"sqlite:///{LOCAL_DB_PATH}", future=True)
    target_dsn = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    target_engine = create_engine(target_dsn, future=True)

    source_desc = f"{source_engine.url.host}/{source_engine.url.database}" if source_url else f"{LOCAL_DB_PATH} (read-only)"
    print(f"Source: {source_desc}")
    print(f"Target: {target_engine.url.host}/{target_engine.url.database}")

    metadata.create_all(target_engine)

    results = []
    for table in metadata.tables.values():
        with source_engine.connect() as source_conn:
            rows = [dict(r) for r in source_conn.execute(select(table)).mappings().all()]
        source_count = len(rows)

        pk_cols = list(table.primary_key.columns)
        with target_engine.connect() as target_conn:
            already_migrated = len(target_conn.execute(select(table)).fetchall())

        if already_migrated == source_count and source_count > 0:
            print(f"  {table.name}: already migrated (source={source_count}), skipping")
            results.append((table.name, source_count, already_migrated, "OK"))
            continue

        if already_migrated > 0:
            # The live system was still writing to the source (an append-only
            # table like predictions/trade_intents can legitimately grow
            # between migration runs — confirmed live 2026-08-14, this is why
            # the scheduled task gets paused before the *final* run). For a
            # single-integer-PK table this is safe to top up incrementally
            # rather than requiring an exact match: insert only source rows
            # whose PK isn't already present in the target.
            if len(pk_cols) == 1 and isinstance(pk_cols[0].type, Integer):
                pk_name = pk_cols[0].name
                with target_engine.connect() as target_conn:
                    existing_ids = {r[0] for r in target_conn.execute(select(pk_cols[0])).fetchall()}
                rows = [r for r in rows if r[pk_name] not in existing_ids]
                print(f"  {table.name}: target has {already_migrated}, topping up {len(rows)} new row(s)")
            else:
                raise SystemExit(
                    f"{table.name}: target already has {already_migrated} rows but source has "
                    f"{source_count}, and this table has no single-integer primary key to safely "
                    f"top up against — partial/inconsistent state, not safe to auto-resume. "
                    f"Clear this table in Postgres and re-run."
                )

        if rows:
            rows = [_normalize_row(r, table) for r in rows]
            with target_engine.begin() as target_conn:
                for i in range(0, len(rows), BATCH_SIZE):
                    target_conn.execute(table.insert(), rows[i:i + BATCH_SIZE])
        if (source_count and len(pk_cols) == 1 and isinstance(pk_cols[0].type, Integer)
                and pk_cols[0].autoincrement is not False):
            pk_name = pk_cols[0].name
            with target_engine.begin() as target_conn:
                target_conn.execute(text(
                    f"SELECT setval(pg_get_serial_sequence('{table.name}', '{pk_name}'), "
                    f"COALESCE((SELECT MAX({pk_name}) FROM {table.name}), 1), true)"
                ))

        with target_engine.connect() as target_conn:
            target_count = len(target_conn.execute(select(table)).fetchall())

        status = "OK" if source_count == target_count else "MISMATCH"
        results.append((table.name, source_count, target_count, status))
        print(f"  {table.name}: source={source_count} target={target_count} {status}")

    print("\n--- Summary ---")
    mismatches = [r for r in results if r[3] != "OK"]
    total_source = sum(r[1] for r in results)
    total_target = sum(r[2] for r in results)
    print(f"Total rows: source={total_source} target={total_target}")
    if mismatches:
        print(f"MISMATCHES in {len(mismatches)} table(s): {[m[0] for m in mismatches]}")
        raise SystemExit(1)
    print("All tables match. Migration verified.")


if __name__ == "__main__":
    main()

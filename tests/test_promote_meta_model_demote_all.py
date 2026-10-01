"""Unit test for promote_meta_model.py's demote_all() (added 2026-10-01,
used to undeploy the forex-only meta-model once forex trading stopped --
see docs/REQUIREMENT_TRACKER.md / memory for context). Isolated in-memory
SQLite engine -- never the real production DB.

Run: .venv/Scripts/python.exe -m pytest tests/test_promote_meta_model_demote_all.py -v
"""
import os
import sys
from datetime import datetime, timezone

PROJECT_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import create_engine, insert, select

from src.data.db import metadata
from src.data.db import model_registry as model_registry_table
from src.models.promote_meta_model import demote_all


def _fresh_engine():
    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    return engine


def _seed_version(engine, version, deployed):
    with engine.begin() as conn:
        conn.execute(
            insert(model_registry_table).values(
                name="meta_model", version=version, trained_at=datetime.now(timezone.utc), deployed=deployed,
            )
        )


def test_demote_all_undeploys_the_currently_deployed_version():
    engine = _fresh_engine()
    _seed_version(engine, "v1", deployed=False)
    _seed_version(engine, "v2", deployed=True)

    demote_all(engine)

    with engine.connect() as conn:
        rows = conn.execute(select(model_registry_table.c.version, model_registry_table.c.deployed)).all()
    assert all(not deployed for _, deployed in rows)


def test_demote_all_is_a_no_op_when_nothing_is_deployed():
    engine = _fresh_engine()
    _seed_version(engine, "v1", deployed=False)

    demote_all(engine)  # must not raise

    with engine.connect() as conn:
        rows = conn.execute(select(model_registry_table.c.deployed)).all()
    assert all(not deployed for (deployed,) in rows)

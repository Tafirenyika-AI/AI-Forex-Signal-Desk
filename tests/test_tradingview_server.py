"""V4 Priority 8 — src/integrations/tradingview_server.py.

End-to-end tests via FastAPI's TestClient (real HTTP request/response
cycle, in-process) against an isolated in-memory SQLite engine — never
the real production database.

Run: .venv/Scripts/python.exe -m pytest tests/test_tradingview_server.py -v
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from src.data.db import metadata
from src.data.db import tradingview_alerts as tradingview_alerts_table
from src.integrations import tradingview_server

SECRET = "real-shared-secret-value"


@pytest.fixture()
def client(monkeypatch):
    # StaticPool: the FastAPI TestClient exercises the app through a
    # different connection checkout than this test's own verification
    # queries -- plain "sqlite:///:memory:" gives each checkout a
    # genuinely separate, blank in-memory database unless pinned to one
    # shared connection via StaticPool.
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    metadata.create_all(engine)
    tradingview_server.app.state.engine = engine
    monkeypatch.setenv("TRADINGVIEW_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("V4_TRADINGVIEW_ENABLED", "true")
    yield TestClient(tradingview_server.app), engine
    tradingview_server.app.state.engine = None


def _payload(**overrides):
    base = {
        "secret": SECRET, "symbol": "NASDAQ:AAPL", "action": "buy",
        "time": datetime.now(timezone.utc).isoformat(), "strategy": "test_strategy",
    }
    base.update(overrides)
    return base


def test_disabled_flag_rejects_every_request_and_persists_nothing(client, monkeypatch):
    test_client, engine = client
    monkeypatch.setenv("V4_TRADINGVIEW_ENABLED", "false")
    response = test_client.post("/webhook/tradingview", json=_payload())
    assert response.status_code == 503
    with engine.connect() as conn:
        count = conn.execute(select(tradingview_alerts_table)).fetchall()
    assert count == []  # not even a rejected-alert audit row


def test_valid_alert_is_accepted_and_persisted(client):
    test_client, engine = client
    response = test_client.post("/webhook/tradingview", json=_payload())
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True
    assert body["symbol"] == "AAPL"

    with engine.connect() as conn:
        rows = conn.execute(select(tradingview_alerts_table)).mappings().all()
    assert len(rows) == 1
    assert rows[0]["accepted"] is True
    assert rows[0]["symbol_normalized"] == "AAPL"


def test_wrong_secret_is_rejected_with_400_and_audit_logged(client):
    test_client, engine = client
    response = test_client.post("/webhook/tradingview", json=_payload(secret="wrong"))
    assert response.status_code == 400
    assert response.json()["accepted"] is False

    with engine.connect() as conn:
        rows = conn.execute(select(tradingview_alerts_table)).mappings().all()
    assert len(rows) == 1
    assert rows[0]["accepted"] is False
    assert "secret" in rows[0]["rejection_reason"]


def test_duplicate_alert_is_persisted_only_once(client):
    test_client, engine = client
    payload = _payload()
    r1 = test_client.post("/webhook/tradingview", json=payload)
    r2 = test_client.post("/webhook/tradingview", json=payload)  # exact same payload, resent
    assert r1.status_code == 200
    assert r2.status_code == 200  # the duplicate is still a valid, accepted alert from the caller's POV

    with engine.connect() as conn:
        rows = conn.execute(select(tradingview_alerts_table)).mappings().all()
    assert len(rows) == 1  # but the DB only ever keeps one real row for it


def test_non_json_body_is_rejected_with_400(client):
    test_client, engine = client
    response = test_client.post(
        "/webhook/tradingview", content=b"not json", headers={"content-type": "application/json"},
    )
    assert response.status_code == 400


def test_invalid_action_is_rejected_and_audited(client):
    test_client, engine = client
    response = test_client.post("/webhook/tradingview", json=_payload(action="moon"))
    assert response.status_code == 400
    with engine.connect() as conn:
        rows = conn.execute(select(tradingview_alerts_table)).mappings().all()
    assert len(rows) == 1
    assert rows[0]["accepted"] is False

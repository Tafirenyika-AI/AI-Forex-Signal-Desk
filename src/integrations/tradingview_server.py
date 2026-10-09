"""V4 Priority 8 (brief Section 17) — the real HTTPS receiver.

A minimal FastAPI app exposing one endpoint, POST /webhook/tradingview.
Delegates all real decision logic to src/integrations/tradingview_webhook.py
(kept separate and dependency-free of FastAPI so it's directly unit-
testable). Every request is persisted to the `tradingview_alerts` table —
accepted or rejected — as a real audit log, not just accepted ones.

Ships OFF by default, per the brief's own explicit instruction: gated on
src/v4/feature_flags.py's v4_tradingview_enabled() (V4_TRADINGVIEW_ENABLED,
defaults false) — when disabled, every request is rejected with a clear
503 and nothing is written to the event queue/log at all (not even a
rejected-alert audit row), since an operator who hasn't turned this on
yet shouldn't have their database quietly accumulating traffic for a
feature they never asked for.

This server does not run by default alongside anything else in this
project (no existing process starts it) — an operator who wants it live
runs `python -m src.integrations.tradingview_server` explicitly, and is
responsible for actually exposing it over real HTTPS (a reverse proxy/
TLS termination/public DNS are deployment decisions outside what this
module can or should do on someone's behalf) and setting a real
TRADINGVIEW_WEBHOOK_SECRET before pointing a real TradingView alert at it.

Deliberately does NOT use official or unofficial TradingView broker
endpoints, and never creates a trade_intent, calls a broker, or feeds any
live decision path — exactly the brief's own "supplementary evidence, not
automatic commands to trade" instruction, enforced structurally (there is
no code path here that CAN place an order), not just by policy.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from src.config import load_settings
from src.data.db import get_engine
from src.data.db import tradingview_alerts as tradingview_alerts_table
from src.data.db import upsert_insert as insert
from src.integrations.tradingview_webhook import validate_and_normalize_alert
from src.v4.feature_flags import v4_tradingview_enabled

app = FastAPI(title="TradingView webhook receiver (V4 Priority 8)")
app.state.engine = None  # lazily set to the real engine on first request; tests substitute their own isolated engine directly


def _get_engine():
    if app.state.engine is None:
        settings = load_settings()
        app.state.engine = get_engine(settings.db_path)
    return app.state.engine


def _persist(result, raw_payload: dict, received_at: datetime) -> None:
    engine = _get_engine()
    with engine.begin() as conn:
        stmt = insert(tradingview_alerts_table).values(
            received_at=received_at, raw_payload_json=json.dumps(raw_payload),
            accepted=result.accepted, rejection_reason=result.rejection_reason,
            symbol_normalized=result.symbol_normalized, action=result.action,
            alert_time=result.alert_time, strategy_name=result.strategy_name,
            dedup_key=result.dedup_key,
        )
        if result.dedup_key is not None:
            stmt = stmt.on_conflict_do_nothing(index_elements=["dedup_key"])
        conn.execute(stmt)


@app.post("/webhook/tradingview")
async def receive_tradingview_alert(request: Request) -> JSONResponse:
    if not v4_tradingview_enabled():
        return JSONResponse(
            status_code=503,
            content={"accepted": False, "reason": "TradingView integration is disabled (V4_TRADINGVIEW_ENABLED=false)"},
        )

    received_at = datetime.now(timezone.utc)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "request body is not valid JSON"})
    if not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "request body must be a JSON object"})

    expected_secret = os.environ.get("TRADINGVIEW_WEBHOOK_SECRET", "").strip() or None
    result = validate_and_normalize_alert(payload, received_at, expected_secret)
    _persist(result, payload, received_at)

    if not result.accepted:
        return JSONResponse(status_code=400, content={"accepted": False, "reason": result.rejection_reason})
    return JSONResponse(status_code=200, content={
        "accepted": True, "symbol": result.symbol_normalized, "action": result.action,
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8766)

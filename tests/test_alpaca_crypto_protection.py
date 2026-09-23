"""Unit tests for P0-04 (external review, 2026-09-22): src/broker/
alpaca.py's crypto entry/stop-loss/take-profit handling.

Uses a real AlpacaBroker instance with _request() monkeypatched (no real
network/credentials) -- proves the actual place_order() code path, not a
reimplementation of it.

Run: .venv/Scripts/python.exe -m pytest tests/test_alpaca_crypto_protection.py -v
"""
import asyncio
import logging
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

from src.broker.alpaca import AlpacaBroker
from src.config import Settings


def _fake_settings():
    return Settings(
        oanda_api_token="fake", oanda_environment="practice", oanda_account_id="fake",
        db_path=Path(":memory:"), fred_api_key=None, alphavantage_api_key=None,
        alpaca_api_key="fake", alpaca_api_secret="fake", alpaca_base_url="https://paper-api.alpaca.markets/v2",
    )


def _run(coro):
    return asyncio.run(coro)


def test_stop_attachment_exception_does_not_corrupt_entry_result(caplog):
    # Real bug found 2026-09-22: an exception while attaching the crypto
    # stop-loss (AFTER a genuine fill) used to propagate all the way up
    # and get caught by ExecutionService.execute()'s own try/except,
    # which then overwrote the ENTRY order's own real "filled" result
    # with status="ERROR:...". Proves the entry result now survives
    # intact regardless of what happens during stop attachment.
    broker = AlpacaBroker(_fake_settings())
    calls = []

    async def fake_request(client, method, path, **kwargs):
        calls.append((method, path))
        if method == "POST" and path == "/orders" and "json" in kwargs and kwargs["json"].get("client_order_id") == "test-entry":
            return {"id": "entry-1", "status": "accepted"}
        if method == "GET" and path == "/orders/entry-1":
            return {"status": "filled"}
        if method == "GET" and path == "/positions/BTCUSD":
            return {"qty_available": "0.25"}
        if method == "POST" and path == "/orders":  # the stop-loss placement itself
            raise RuntimeError("simulated transient network failure placing stop")
        raise AssertionError(f"unexpected call: {method} {path}")

    broker._request = fake_request
    with caplog.at_level(logging.ERROR):
        result = _run(broker.place_order(
            instrument="BTC/USD", units=0.25, client_order_id="test-entry",
            stop_loss_price=59000.0,
        ))

    assert result.status == "accepted"  # the ENTRY's own real status, not "ERROR:..."
    assert result.broker_order_id == "entry-1"
    assert any("UNPROTECTED" in r.message for r in caplog.records)


def test_take_profit_supplied_for_crypto_logs_loud_warning_not_silent_drop(caplog):
    broker = AlpacaBroker(_fake_settings())

    async def fake_request(client, method, path, **kwargs):
        if method == "POST" and path == "/orders":
            return {"id": "entry-2", "status": "accepted"}
        raise AssertionError(f"unexpected call: {method} {path}")

    broker._request = fake_request
    with caplog.at_level(logging.WARNING):
        result = _run(broker.place_order(
            instrument="BTC/USD", units=0.1, client_order_id="test-entry-2",
            take_profit_price=65000.0,  # no stop_loss_price -- isolates the take-profit path
        ))

    assert result.status == "accepted"
    assert any("take_profit_price" in r.message and "NOT" in r.message for r in caplog.records)


def test_no_stop_no_take_profit_no_warnings(caplog):
    broker = AlpacaBroker(_fake_settings())

    async def fake_request(client, method, path, **kwargs):
        return {"id": "entry-3", "status": "accepted"}

    broker._request = fake_request
    with caplog.at_level(logging.WARNING):
        result = _run(broker.place_order(instrument="BTC/USD", units=0.1, client_order_id="test-entry-3"))

    assert result.status == "accepted"
    assert not any("UNPROTECTED" in r.message or "take_profit_price" in r.message for r in caplog.records)

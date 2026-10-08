"""V4 Phase 0 safety audit — real bug found live 2026-10-08:
src/broker/alpaca.py's get_equity_stop_price() queried orders with
status="open", which silently excludes a real, live, genuinely
protective stop-loss order whose sibling take-profit leg is still
resting -- Alpaca reports THAT stop leg's own status as "held", not
"open"/"new". Confirmed directly against this account's real AAPL/MSFT
short positions: both have real, live buy-stop orders at a real
stop_price, both status="held", both invisible to the old filter. That
made the risk governor's correlation gate (src/run_loop.py's
compute_correlated_stop_risk) treat two genuinely protected real
positions as having float('inf') unbounded risk.

Uses a real AlpacaBroker instance with _request() monkeypatched (no real
network/credentials) -- proves the actual get_equity_stop_price() code
path, not a reimplementation of it. Same pattern as
tests/test_alpaca_crypto_protection.py.

Run: .venv/Scripts/python.exe -m pytest tests/test_alpaca_equity_stop_price_status_bug.py -v
"""
import asyncio
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


def test_held_status_stop_order_is_found_not_treated_as_missing():
    # Real captured shape from this account's actual AAPL position,
    # 2026-10-08 (see broker.positions()/orders() -- a real live stop
    # order with status="held").
    broker = AlpacaBroker(_fake_settings())

    async def fake_request(client, method, path, **kwargs):
        assert path == "/orders"
        assert kwargs["params"]["status"] == "all"  # not "open" -- that's the fix
        return [
            {"symbol": "AAPL", "type": "limit", "side": "buy", "status": "new", "stop_price": None},  # the take-profit leg
            {"symbol": "AAPL", "type": "stop", "side": "buy", "status": "held", "stop_price": "339.70"},  # the real protective stop
            {"symbol": "AAPL", "type": "stop", "side": "sell", "status": "canceled", "stop_price": "100.0"},  # old, dead, must be ignored
        ]

    broker._request = fake_request
    result = _run(broker.get_equity_stop_price("AAPL"))
    assert result == 339.70


def test_terminal_status_stop_orders_are_excluded():
    broker = AlpacaBroker(_fake_settings())

    async def fake_request(client, method, path, **kwargs):
        return [
            {"symbol": "AAPL", "type": "stop", "side": "buy", "status": "canceled", "stop_price": "339.70"},
            {"symbol": "AAPL", "type": "stop", "side": "buy", "status": "filled", "stop_price": "340.00"},
            {"symbol": "AAPL", "type": "stop", "side": "buy", "status": "expired", "stop_price": "341.00"},
        ]

    broker._request = fake_request
    result = _run(broker.get_equity_stop_price("AAPL"))
    assert result is None  # every real candidate is genuinely dead -- honestly unprotected


def test_genuinely_no_stop_order_at_all_returns_none():
    broker = AlpacaBroker(_fake_settings())

    async def fake_request(client, method, path, **kwargs):
        return []

    broker._request = fake_request
    result = _run(broker.get_equity_stop_price("AAPL"))
    assert result is None


def test_a_new_status_stop_order_is_still_found_same_as_before_the_fix():
    # Non-regression: the common, simple case (a brand-new order, no
    # resting sibling) must keep working exactly as it did before.
    broker = AlpacaBroker(_fake_settings())

    async def fake_request(client, method, path, **kwargs):
        return [{"symbol": "AAPL", "type": "stop", "side": "buy", "status": "new", "stop_price": "339.70"}]

    broker._request = fake_request
    result = _run(broker.get_equity_stop_price("AAPL"))
    assert result == 339.70

"""Unit tests for src/run_loop.py's compute_correlated_stop_risk, using
fake broker objects (no real network/broker calls) — covers both the
OANDA open_trades() path and the Alpaca per-position stop-lookup path,
including the "unprotected position -> inf" fail-closed behavior.

Run: .venv/Scripts/python.exe -m pytest tests/test_compute_correlated_stop_risk.py -v
"""
import asyncio
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

from src.run_loop import compute_correlated_stop_risk


class _FakeOandaBroker:
    def __init__(self, trades):
        self._trades = trades

    async def open_trades(self):
        return self._trades


class _FakeAlpacaBroker:
    def __init__(self, stops: dict):
        self._stops = stops  # instrument -> stop price (or None)

    async def get_position_stop_price(self, instrument):
        return self._stops.get(instrument)


def _run(coro):
    return asyncio.run(coro)


def test_oanda_protected_trade_computes_risk_at_stop():
    # Long 100,000 USD_JPY at 150, stop at 149.5 -> risk = 0.5 * 100000 * (1/150)
    broker = _FakeOandaBroker([{
        "instrument": "USD_JPY", "currentUnits": "100000", "price": "150.000",
        "stopLossOrder": {"price": "149.500"},
    }])
    risk = _run(compute_correlated_stop_risk(broker, "oanda", [], "demo"))
    assert risk["long_usd"] == pytest_approx(0.5 * 100_000 * (1 / 150.0))


def test_oanda_unprotected_trade_is_infinite_risk():
    broker = _FakeOandaBroker([{
        "instrument": "USD_JPY", "currentUnits": "100000", "price": "150.000",
        "stopLossOrder": None,
    }])
    risk = _run(compute_correlated_stop_risk(broker, "oanda", [], "demo"))
    assert risk["long_usd"] == float("inf")


def test_oanda_multiple_trades_same_bucket_sum():
    broker = _FakeOandaBroker([
        {"instrument": "USD_JPY", "currentUnits": "100000", "price": "150.000",
         "stopLossOrder": {"price": "149.500"}},
        {"instrument": "USD_CHF", "currentUnits": "50000", "price": "0.900",
         "stopLossOrder": {"price": "0.895"}},
    ])
    risk = _run(compute_correlated_stop_risk(broker, "oanda", [], "demo"))
    # USD_CHF: base=USD too, so usd_value_per_unit is 1/price (0.9), not 1.0.
    expected = 0.5 * 100_000 * (1 / 150.0) + 0.005 * 50_000 * (1 / 0.9)
    assert risk["long_usd"] == pytest_approx(expected)


def test_oanda_no_usd_leg_trade_excluded():
    # AUD_CAD has no direct USD leg -- excluded from every bucket.
    broker = _FakeOandaBroker([{
        "instrument": "AUD_CAD", "currentUnits": "10000", "price": "1.100",
        "stopLossOrder": {"price": "1.090"},
    }])
    risk = _run(compute_correlated_stop_risk(broker, "oanda", [], "demo"))
    assert risk == {}


def test_alpaca_equity_protected_position():
    broker = _FakeAlpacaBroker({"AAPL": 195.0})
    positions_raw = [{"symbol": "AAPL", "qty": "100", "side": "long", "avg_entry_price": "200.00"}]
    risk = _run(compute_correlated_stop_risk(broker, "alpaca", positions_raw, "demo"))
    assert risk["equity_long"] == pytest_approx(5.0 * 100)


def test_alpaca_equity_unprotected_position_is_infinite():
    broker = _FakeAlpacaBroker({"AAPL": None})
    positions_raw = [{"symbol": "AAPL", "qty": "100", "side": "long", "avg_entry_price": "200.00"}]
    risk = _run(compute_correlated_stop_risk(broker, "alpaca", positions_raw, "demo"))
    assert risk["equity_long"] == float("inf")


def test_alpaca_mixed_protected_and_unprotected_same_bucket_is_infinite():
    broker = _FakeAlpacaBroker({"AAPL": 195.0, "MSFT": None})
    positions_raw = [
        {"symbol": "AAPL", "qty": "100", "side": "long", "avg_entry_price": "200.00"},
        {"symbol": "MSFT", "qty": "50", "side": "long", "avg_entry_price": "400.00"},
    ]
    risk = _run(compute_correlated_stop_risk(broker, "alpaca", positions_raw, "demo"))
    assert risk["equity_long"] == float("inf")


def pytest_approx(value, rel=1e-9):
    import pytest
    return pytest.approx(value, rel=rel)

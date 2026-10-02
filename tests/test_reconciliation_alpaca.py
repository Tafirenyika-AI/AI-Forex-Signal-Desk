"""Unit tests for Equity V2 Phase 1: src/reconciliation/alpaca.py.

Uses an isolated in-memory SQLite engine (never the real production DB,
this system has live --auto-execute trading) and a fake AlpacaBroker with
only _request()/account_state()/positions() monkeypatched — proves the
actual reconcile() code path, not a reimplementation of it, matching this
project's established testing pattern (see tests/test_alpaca_crypto_
protection.py's own docstring for the same approach).

Run: .venv/Scripts/python.exe -m pytest tests/test_reconciliation_alpaca.py -v
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

PROJECT_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import create_engine, insert, select

from src.broker.alpaca import AlpacaBroker
from src.broker.base import AccountState
from src.config import Settings
from src.data.db import metadata
from src.data.db import orders_fills as orders_fills_table
from src.data.db import positions_snapshots as positions_snapshots_table
from src.data.db import reconciliation_issues as reconciliation_issues_table
from src.data.db import trade_outcomes as trade_outcomes_table
from src.reconciliation.alpaca import (
    STARTING_DEPOSIT,
    _check_pnl,
    _check_positions,
    _check_protective_orders,
    _check_unexplained_orders,
    _is_known_client_order_id,
    _net_position,
    reconcile,
)

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _run(coro):
    return asyncio.run(coro)


def _fresh_engine():
    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    return engine


def _fake_settings():
    from pathlib import Path
    return Settings(
        oanda_api_token="fake", oanda_environment="practice", oanda_account_id="fake",
        db_path=Path(":memory:"), fred_api_key=None, alphavantage_api_key=None,
        alpaca_api_key="fake", alpaca_api_secret="fake", alpaca_base_url="https://paper-api.alpaca.markets/v2",
    )


def _fill(side, qty, price, minutes_offset, order_id, client_order_id=None):
    return {"side": side, "qty": qty, "price": price,
            "time": NOW - timedelta(days=1) + timedelta(minutes=minutes_offset),
            "order_id": order_id, "client_order_id": client_order_id}


# --- pure helpers ---

def test_net_position_long():
    fills = [_fill("buy", 100, 10.0, 0, "o1"), _fill("buy", 50, 11.0, 10, "o2")]
    assert _net_position(fills) == 150


def test_net_position_round_trip_to_flat():
    fills = [_fill("buy", 100, 10.0, 0, "o1"), _fill("sell", 100, 11.0, 10, "o2")]
    assert _net_position(fills) == 0


def test_net_position_short():
    fills = [_fill("sell", 40, 10.0, 0, "o1")]
    assert _net_position(fills) == -40


def test_is_known_client_order_id():
    assert _is_known_client_order_id("intent-12345")
    assert _is_known_client_order_id("MSFT-protective-stop-6812547e")
    assert _is_known_client_order_id("BTCUSD-trail-abcd1234")
    assert _is_known_client_order_id("NVDA-BUY-3c75e891a0c2-stop")
    assert not _is_known_client_order_id("1e983c0a-83c5-4ea6-8d43-8f889c731a57")
    assert not _is_known_client_order_id(None)


# --- _check_positions ---

def test_check_positions_match_is_verified():
    fills_by_symbol = {"MSFT": [_fill("sell", 100, 500.0, 0, "o1")]}
    issues = _check_positions({"MSFT": -100.0}, fills_by_symbol, NOW)
    assert len(issues) == 1
    assert issues[0].severity == "VERIFIED"
    assert issues[0].issue_type == "position_match"


def test_check_positions_tolerates_real_crypto_fee_dust():
    # The exact real pattern found live 2026-10-01: 7 real BTC/USD round
    # trips, each buying slightly more than was later sellable (Alpaca
    # deducts crypto fees from the position itself) -- broker position is
    # genuinely, exactly flat; naive net-quantity summation says a tiny
    # 5.181e-6 BTC is still "open." Must be tolerated for crypto, not
    # flagged as a real mismatch.
    fills = [
        _fill("buy", 0.000314981, 77845.9, 0, "o1"), _fill("sell", 0.000314193, 77793.1, 1, "o2"),
        _fill("buy", 0.0003, 77945.4, 2, "o3"), _fill("sell", 0.00029925, 77968.8, 3, "o4"),
        _fill("buy", 0.0003, 78048.4, 4, "o5"), _fill("sell", 0.00029925, 77959.2, 5, "o6"),
        _fill("buy", 0.0003, 77980.2, 6, "o7"), _fill("sell", 0.00029925, 77882.9, 7, "o8"),
        _fill("buy", 0.0003, 78632.0, 8, "o9"), _fill("sell", 0.00029925, 78635.8, 9, "o10"),
        _fill("buy", 0.0003, 78453.9, 10, "o11"), _fill("sell", 0.00029925, 78401.2, 11, "o12"),
        _fill("buy", 0.000257, 78000.1, 12, "o13"), _fill("sell", 0.000256357, 77956.6, 13, "o14"),
    ]
    issues = _check_positions({"BTC/USD": 0.0}, {"BTC/USD": fills}, NOW)
    assert len(issues) == 1
    assert issues[0].severity == "VERIFIED"
    assert "fee-dust tolerance" in issues[0].description


def test_check_positions_mismatch_is_critical():
    fills_by_symbol = {"MSFT": [_fill("sell", 100, 500.0, 0, "o1")]}
    issues = _check_positions({"MSFT": -50.0}, fills_by_symbol, NOW)  # broker disagrees
    assert len(issues) == 1
    assert issues[0].severity == "CRITICAL"
    assert issues[0].issue_type == "position_mismatch"
    assert "-50" in issues[0].broker_value
    assert "-100" in issues[0].internal_value


def test_check_positions_equity_does_not_get_crypto_dust_tolerance():
    # A gap this small (0.00001 shares) would pass under crypto's fee-dust
    # tolerance but must NOT for equities -- no comparable fee mechanic
    # exists there, so any nonzero gap this size is still a real signal.
    fills_by_symbol = {"MSFT": [_fill("buy", 100.00001, 500.0, 0, "o1")]}
    issues = _check_positions({"MSFT": 100.0}, fills_by_symbol, NOW)
    assert len(issues) == 1
    assert issues[0].severity == "CRITICAL"


# --- _check_protective_orders ---

def test_check_protective_orders_present_is_verified():
    open_orders = [{"symbol": "MSFT", "type": "stop"}]
    issues = _check_protective_orders(None, {"MSFT": -676.0}, open_orders, NOW)
    assert len(issues) == 1
    assert issues[0].severity == "VERIFIED"


def test_check_protective_orders_missing_is_critical():
    # The exact real bug found live 2026-10-01: a real open position with
    # zero protective orders at all.
    issues = _check_protective_orders(None, {"MSFT": -676.0}, [], NOW)
    assert len(issues) == 1
    assert issues[0].severity == "CRITICAL"
    assert issues[0].issue_type == "unprotected_position"


def test_check_protective_orders_ignores_flat_positions():
    issues = _check_protective_orders(None, {"MSFT": 0.0}, [], NOW)
    assert issues == []


# --- _check_unexplained_orders ---

def test_check_unexplained_orders_flags_unrecognized_fill():
    filled = [{"id": "o1", "symbol": "NVDA", "side": "sell", "filled_qty": "652",
               "filled_avg_price": "231.63", "client_order_id": "1e983c0a-83c5-4ea6-8d43-8f889c731a57"}]
    issues = _check_unexplained_orders(filled, known_order_coids=set(), now=NOW)
    assert len(issues) == 1
    assert issues[0].severity == "UNRESOLVED"
    assert issues[0].issue_type == "unexplained_broker_order"


def test_check_unexplained_orders_skips_known_convention():
    filled = [{"id": "o1", "symbol": "BTC/USD", "side": "sell", "filled_qty": "0.001",
               "filled_avg_price": "78000", "client_order_id": "BTCUSD-stop-abcd1234"}]
    assert _check_unexplained_orders(filled, known_order_coids=set(), now=NOW) == []


def test_check_unexplained_orders_skips_known_orders_fills_row():
    filled = [{"id": "o1", "symbol": "MSFT", "side": "sell", "filled_qty": "45",
               "filled_avg_price": "516.01", "client_order_id": "some-uuid-1234"}]
    issues = _check_unexplained_orders(filled, known_order_coids={"some-uuid-1234"}, now=NOW)
    assert issues == []


# --- _check_pnl ---

def test_check_pnl_within_tolerance_is_verified():
    issues = _check_pnl(nav=109073.57, deposited=100000.0, internal_realized=7827.85,
                         internal_unrealized=1245.72, now=NOW)
    assert len(issues) == 1
    assert issues[0].severity == "VERIFIED"


def test_check_pnl_large_gap_is_critical():
    # The exact real pre-fix gap found live: ~$9,400 unexplained.
    issues = _check_pnl(nav=109099.66, deposited=100000.0, internal_realized=-1589.80,
                         internal_unrealized=1279.52, now=NOW)
    assert len(issues) == 1
    assert issues[0].severity == "CRITICAL"


def test_check_pnl_moderate_gap_is_warning():
    issues = _check_pnl(nav=100500.0, deposited=100000.0, internal_realized=200.0,
                         internal_unrealized=0.0, now=NOW)
    assert len(issues) == 1
    assert issues[0].severity == "WARNING"


# --- full reconcile() end to end ---

def test_reconcile_end_to_end_persists_and_reports():
    engine = _fresh_engine()
    broker = AlpacaBroker(_fake_settings())

    with engine.begin() as conn:
        conn.execute(insert(trade_outcomes_table).values(
            user_id=1, trade_intent_id=None, client_order_id="c1", broker_trade_id="t1",
            execution_mode="demo", instrument="MSFT", action="SELL", units=350.0,
            entry_price=490.5, exit_price=492.29, realized_pl_usd=-626.5,
            opened_at=NOW, closed_at=NOW, outcome="LOSS", synced_at=NOW, broker="alpaca",
        ))
        conn.execute(insert(orders_fills_table).values(
            user_id=1, time=NOW, client_order_id="intent-1", broker_order_id="bo1",
            broker_transaction_id=None, instrument="NVDA", units=652.0, status="pending_new",
            broker="alpaca", execution_mode="demo",
        ))

    async def fake_request(client, method, path, **kwargs):
        if path == "/orders" and kwargs.get("params", {}).get("status") == "open":
            return []  # no open orders -- NVDA position below will be flagged unprotected
        if path == "/orders":  # status=all
            return [
                {"id": "o1", "symbol": "NVDA", "side": "buy", "status": "filled",
                 "filled_at": "2026-09-10T18:32:27.313317Z", "filled_avg_price": "217.98",
                 "filled_qty": "652", "client_order_id": "intent-1"},
            ]
        raise AssertionError(f"unexpected call: {method} {path}")

    broker._request = fake_request

    async def fake_account_state():
        return AccountState(
            account_id="PA_TEST", currency="USD", balance=109000.0, nav=109000.0,
            unrealized_pl=1000.0, margin_used=0.0, margin_available=109000.0,
            open_trade_count=1, open_position_count=1,
        )
    broker.account_state = fake_account_state

    async def fake_positions():
        return [{"symbol": "NVDA", "qty": "652", "side": "long", "unrealized_pl": "1000.0"}]
    broker.positions = fake_positions

    report = _run(reconcile(engine, broker, user_id=1))

    assert report.broker_verified_nav == 109000.0
    assert report.broker_verified_deposited == STARTING_DEPOSIT["alpaca"]
    assert report.internal_realized_pl == -626.5
    assert report.internal_unrealized_pl == 1000.0

    severities = {i.issue_type: i.severity for i in report.issues}
    assert severities["position_match"] == "VERIFIED"  # 652 broker == 652 reconstructed
    assert severities["unprotected_position"] == "CRITICAL"  # no open orders at all

    with engine.connect() as conn:
        persisted = conn.execute(select(reconciliation_issues_table)).mappings().all()
        snapshots = conn.execute(select(positions_snapshots_table)).mappings().all()
    assert len(persisted) == len(report.issues)
    assert len(snapshots) == 1
    assert snapshots[0]["nav"] == 109000.0

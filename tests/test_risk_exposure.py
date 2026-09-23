"""Unit tests for USD notional exposure math — src/risk/governor.py's
usd_notional_per_unit and src/run_loop.py's compute_exposure.

Covers T01/T02 from the 2026-09-22 external review brief: the exposure
function used to reuse usd_value_per_unit (a *different* quantity, P&L per
1-unit price move, correct for stop-distance risk sizing) as if it were
notional-per-unit, which is only correct for quote-is-USD FX pairs. This
silently broke MAX_EQUITY_CRYPTO_NOTIONAL_PCT's no-leverage headroom check
for equities/crypto (comparing share counts against a dollar headroom).

Run: .venv/Scripts/python.exe -m pytest tests/test_risk_exposure.py -v
"""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

from src.risk.governor import usd_notional_per_unit
from src.run_loop import compute_exposure


# --- T01: USD_JPY (base=USD) notional ---

def test_t01_usd_base_pair_notional_per_unit_is_one():
    # 1 unit of USD_JPY IS 1 USD (base currency is USD) -- not 1/price.
    assert usd_notional_per_unit("USD_JPY", 150.0) == 1.0


def test_t01_usd_base_pair_full_position_notional():
    positions_raw = [{
        "instrument": "USD_JPY",
        "long": {"units": "100000", "averagePrice": "150.000"},
        "short": {"units": "0"},
    }]
    open_count, exposure = compute_exposure(positions_raw, "oanda", "demo")
    assert open_count == 1
    assert exposure["long_usd"] == 100_000.0  # was ~666.67 before the fix


# --- T02: equity share notional ---

def test_t02_equity_notional_per_unit_is_price():
    # 1 share of a $200 stock is worth $200, not $1.
    assert usd_notional_per_unit("AAPL", 200.0) == 200.0


def test_t02_equity_full_position_notional():
    positions_raw = [{"symbol": "AAPL", "qty": "100", "side": "long", "avg_entry_price": "200.00"}]
    open_count, exposure = compute_exposure(positions_raw, "alpaca", "paper")
    assert open_count == 1
    assert exposure["equity_long"] == 20_000.0  # was 100 before the fix


# --- crypto shares the equity bug (usd_value_per_unit was 1.0 for it too) ---

def test_crypto_notional_per_unit_is_price():
    assert usd_notional_per_unit("BTC/USD", 60_000.0) == 60_000.0


def test_crypto_full_position_notional():
    positions_raw = [{"symbol": "BTC/USD", "qty": "0.5", "side": "long", "avg_entry_price": "60000.0"}]
    open_count, exposure = compute_exposure(positions_raw, "alpaca", "paper")
    assert exposure["crypto_long"] == 30_000.0


# --- quote-is-USD FX pairs: notional-per-unit legitimately equals price (unchanged case) ---

def test_quote_usd_pair_notional_per_unit_is_price():
    assert usd_notional_per_unit("EUR_USD", 1.08) == 1.08


def test_quote_usd_pair_full_position_notional():
    positions_raw = [{
        "instrument": "EUR_USD",
        "long": {"units": "100000", "averagePrice": "1.08000"},
        "short": {"units": "0"},
    }]
    _, exposure = compute_exposure(positions_raw, "oanda", "demo")
    # BUY EUR_USD = long EUR = short USD (see usd_direction_of_trade's docstring).
    assert exposure["short_usd"] == 108_000.0


# --- true cross pair: needs usd_rates for the BASE currency, not the quote ---

def test_cross_pair_notional_uses_base_currency_rate():
    # AUD_CAD: 1 unit = 1 AUD -> converted via usd_rates["AUD"], not usd_rates["CAD"].
    usd_rates = {"AUD": 0.65, "CAD": 0.74}
    assert usd_notional_per_unit("AUD_CAD", 1.10, usd_rates) == 0.65


def test_cross_pair_notional_raises_without_base_rate():
    import pytest
    with pytest.raises(ValueError):
        usd_notional_per_unit("AUD_CAD", 1.10, {"CAD": 0.74})


# --- aggregate: several same-direction equity positions must sum by dollar value ---

def test_aggregate_equity_notional_sums_by_dollar_value_not_share_count():
    positions_raw = [
        {"symbol": "AAPL", "qty": "100", "side": "long", "avg_entry_price": "200.00"},
        {"symbol": "MSFT", "qty": "50", "side": "long", "avg_entry_price": "400.00"},
    ]
    open_count, exposure = compute_exposure(positions_raw, "alpaca", "paper")
    assert open_count == 2
    # 100*200 + 50*400 = 20,000 + 20,000 = 40,000 -- was 100+50=150 before the fix.
    assert exposure["equity_long"] == 40_000.0

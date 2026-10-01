"""Unit tests for the FIFO position-matching engine in
src/outcomes/alpaca_tracker.py (real bug found live 2026-10-01: the old
bracket-leg-only matching missed any position closed by something other
than its own original bracket leg firing -- see that module's docstring).

compute_fifo_outcomes is a pure function (no broker/DB) -- tested directly
with synthetic fills, plus two cases reproducing REAL account history
(the NVDA/MSFT round trips this bug was found from) with hand-verified
expected P&L.

Run: .venv/Scripts/python.exe -m pytest tests/test_alpaca_outcome_fifo.py -v
"""
import os
import sys
from datetime import datetime, timedelta, timezone

PROJECT_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

from src.outcomes.alpaca_tracker import compute_fifo_outcomes

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _fill(side, qty, price, minutes_offset, order_id, client_order_id=None):
    return {
        "side": side, "qty": qty, "price": price,
        "time": T0 + timedelta(minutes=minutes_offset),
        "order_id": order_id, "client_order_id": client_order_id,
    }


def test_simple_round_trip_long():
    fills = [
        _fill("buy", 100, 10.0, 0, "o1", "entry-1"),
        _fill("sell", 100, 12.0, 10, "o2", "exit-1"),
    ]
    outcomes = compute_fifo_outcomes(fills)
    assert len(outcomes) == 1
    o = outcomes[0]
    assert o["action"] == "BUY"
    assert o["units"] == 100
    assert o["entry_price"] == 10.0
    assert o["exit_price"] == 12.0
    assert o["realized_pl_usd"] == 200.0  # (12-10)*100
    assert o["outcome"] == "WIN"
    assert o["entry_client_order_id"] == "entry-1"
    assert o["closing_client_order_id"] == "exit-1"


def test_simple_round_trip_short():
    fills = [
        _fill("sell", 350, 490.5, 0, "o1"),
        _fill("buy", 350, 492.29, 10, "o2"),
    ]
    outcomes = compute_fifo_outcomes(fills)
    assert len(outcomes) == 1
    o = outcomes[0]
    assert o["action"] == "SELL"
    # Hand-verified against the real MSFT trade this bug was found from
    # (trade_outcomes id 977) -- must reproduce the exact same number.
    assert abs(o["realized_pl_usd"] - (-626.5)) < 1e-6
    assert o["outcome"] == "LOSS"


def test_real_nvda_round_trip_reproduces_hand_verified_profit():
    # The exact real trade this whole investigation started from: a
    # +$8,900-ish profit invisible under the old bracket-leg-only logic
    # because it closed via a later, unrelated order after the original
    # bracket's day-limited legs had already expired.
    fills = [
        _fill("buy", 652, 217.98, 0, "entry-nvda"),
        _fill("sell", 652, 231.634126, 60 * 24 * 19, "exit-nvda"),
    ]
    outcomes = compute_fifo_outcomes(fills)
    assert len(outcomes) == 1
    o = outcomes[0]
    assert o["action"] == "BUY"
    assert abs(o["realized_pl_usd"] - 8902.49) < 0.5
    assert o["outcome"] == "WIN"


def test_scale_in_then_single_close_matches_multiple_lots():
    fills = [
        _fill("sell", 45, 516.01, 0, "o1", "a"),
        _fill("sell", 195, 504.56, 10, "o2", "b"),
        _fill("sell", 44, 504.560227, 11, "o3", "c"),
        _fill("buy", 284, 510.0, 20, "o4", "d"),  # closes all three lots at once
    ]
    outcomes = compute_fifo_outcomes(fills)
    assert len(outcomes) == 1
    o = outcomes[0]
    assert o["units"] == 284
    expected_avg_entry = (45 * 516.01 + 195 * 504.56 + 44 * 504.560227) / 284
    assert abs(o["entry_price"] - expected_avg_entry) < 1e-6
    assert o["opened_at"] == T0  # earliest consumed lot
    # Several different entries consumed by one closing fill -- no single
    # entry to credit, must stay unlinked rather than guessing.
    assert o["entry_client_order_id"] is None
    assert o["closing_client_order_id"] == "d"


def test_partial_close_leaves_residual_lot_for_a_later_close():
    fills = [
        _fill("buy", 100, 10.0, 0, "o1", "entry-1"),
        _fill("sell", 40, 11.0, 10, "o2", "exit-partial"),
        _fill("sell", 60, 9.0, 20, "o3", "exit-rest"),
    ]
    outcomes = compute_fifo_outcomes(fills)
    assert len(outcomes) == 2
    first, second = outcomes
    assert first["units"] == 40
    assert abs(first["realized_pl_usd"] - 40.0) < 1e-6  # (11-10)*40
    assert first["entry_client_order_id"] == "entry-1"  # single lot, fully traceable
    assert second["units"] == 60
    assert abs(second["realized_pl_usd"] - (-60.0)) < 1e-6  # (9-10)*60
    assert second["entry_client_order_id"] == "entry-1"


def test_direction_flip_in_one_fill_closes_short_and_opens_long():
    fills = [
        _fill("sell", 100, 10.0, 0, "o1"),   # open short 100
        _fill("buy", 150, 9.0, 10, "o2"),    # closes the 100 short, opens 50 long
        _fill("sell", 50, 11.0, 20, "o3"),   # closes the new 50 long
    ]
    outcomes = compute_fifo_outcomes(fills)
    assert len(outcomes) == 2
    close_short, close_long = outcomes
    assert close_short["action"] == "SELL"
    assert close_short["units"] == 100
    assert abs(close_short["realized_pl_usd"] - 100.0) < 1e-6  # (10-9)*100
    assert close_long["action"] == "BUY"
    assert close_long["units"] == 50
    assert abs(close_long["realized_pl_usd"] - 100.0) < 1e-6  # (11-9)*50


def test_no_closing_fills_produces_no_outcomes():
    fills = [_fill("buy", 100, 10.0, 0, "o1"), _fill("buy", 50, 10.5, 10, "o2")]
    assert compute_fifo_outcomes(fills) == []


def test_empty_fills():
    assert compute_fifo_outcomes([]) == []

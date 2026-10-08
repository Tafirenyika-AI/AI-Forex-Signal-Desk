"""V4 Phase 0 safety audit — real bug found live 2026-10-08 against this
account's actual open positions (AAPL short -400, MSFT short -383):
src/run_loop.py's _normalized_positions() assumed Alpaca's own `qty`
field was always an unsigned magnitude needing a sign applied from
`side` — but Alpaca's real API already returns `qty` pre-signed
(negative for a short position). The old `qty if side=="long" else
-qty` logic double-negated an already-negative real qty back to
positive, making every real short equity position look "long"
everywhere this function's output feeds: compute_exposure's correlation-
gate bucketing AND compute_open_directions_by_instrument's Phase 14
no_pyramid_same_symbol protection.

Every PRE-EXISTING test fixture in this project (tests/
test_risk_exposure.py, tests/test_no_pyramid_gate.py) happened to encode
qty as an unsigned positive string even for a `side: "short"` fixture —
the same wrong assumption the buggy code made, which is exactly why none
of them ever caught this. This file uses the REAL, live-confirmed
Alpaca convention (qty pre-signed) specifically to close that gap.

Run: .venv/Scripts/python.exe -m pytest tests/test_alpaca_short_position_sign_bug.py -v
"""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

from src.run_loop import _normalized_positions, compute_exposure, compute_open_directions_by_instrument


# Real captured shape from this account's actual open positions,
# 2026-10-08 (see src/broker/alpaca.py's positions() -- Alpaca's /v2/
# positions response, qty pre-signed negative for a short).
_REAL_AAPL_SHORT = {"symbol": "AAPL", "qty": "-400", "side": "short", "avg_entry_price": "335.7556"}
_REAL_MSFT_SHORT = {"symbol": "MSFT", "qty": "-383", "side": "short", "avg_entry_price": "532.493733"}
_REAL_LONG = {"symbol": "NVDA", "qty": "138", "side": "long", "avg_entry_price": "180.0"}


def test_normalized_positions_real_short_qty_is_negative_not_double_negated():
    results = dict((instrument, net_units) for instrument, net_units, _ in
                   _normalized_positions([_REAL_AAPL_SHORT], "alpaca", "paper"))
    assert results["AAPL"] == -400.0  # must stay negative -- this is a short


def test_normalized_positions_real_long_is_still_positive():
    results = dict((instrument, net_units) for instrument, net_units, _ in
                    _normalized_positions([_REAL_LONG], "alpaca", "paper"))
    assert results["NVDA"] == 138.0


def test_normalized_positions_handles_a_hypothetical_unsigned_short_too():
    # Defensive: the fix (abs() first, then apply sign from `side`) must
    # also stay correct if a future Alpaca response, or some other code
    # path, ever supplies an unsigned qty for a short -- not just the
    # real pre-signed case this bug was actually found in.
    unsigned_short = {"symbol": "AAPL", "qty": "400", "side": "short", "avg_entry_price": "335.76"}
    results = dict((instrument, net_units) for instrument, net_units, _ in
                    _normalized_positions([unsigned_short], "alpaca", "paper"))
    assert results["AAPL"] == -400.0


def test_compute_exposure_real_short_buckets_as_equity_short_not_equity_long():
    open_count, exposure = compute_exposure([_REAL_AAPL_SHORT, _REAL_MSFT_SHORT], "alpaca", "paper", {})
    assert open_count == 2
    assert exposure["equity_long"] == 0.0
    assert exposure["equity_short"] > 0.0
    expected = 400.0 * 335.7556 + 383.0 * 532.493733
    assert exposure["equity_short"] == expected


def test_compute_open_directions_real_shorts_report_as_short_not_long():
    # This is the exact real consequence found live: before the fix, this
    # returned {"AAPL": "long", "MSFT": "long"} for two real short
    # positions -- silently defeating the Phase 14 no_pyramid_same_symbol
    # gate for exactly these two real positions.
    directions = compute_open_directions_by_instrument([_REAL_AAPL_SHORT, _REAL_MSFT_SHORT], "alpaca", "paper")
    assert directions == {"AAPL": "short", "MSFT": "short"}


def test_compute_open_directions_mixed_real_long_and_short():
    directions = compute_open_directions_by_instrument([_REAL_AAPL_SHORT, _REAL_LONG], "alpaca", "paper")
    assert directions == {"AAPL": "short", "NVDA": "long"}

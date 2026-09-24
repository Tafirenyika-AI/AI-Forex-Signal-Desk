"""Unit test for P1-04 (external review, 2026-09-24): src/scripts/
evaluate_challengers.py's _expiry_price must never finalize a signal's
scored outcome (a PERMANENT, never-re-scored row per the brief's own
"incomplete candles cannot change an already-final evaluation" wording)
using a still-forming candle.

Run: .venv/Scripts/python.exe -m pytest tests/test_evaluate_challengers_expiry.py -v
"""
import asyncio
import os
import sys
from datetime import datetime, timezone

PROJECT_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

from src.broker.base import Candle
from src.run_loop import HORIZON_CONFIGS
from src.scripts.evaluate_challengers import _expiry_price

HORIZON_LABEL = HORIZON_CONFIGS[0].label  # "15m"


class _FakeBroker:
    def __init__(self, candles):
        self._candles = candles

    async def get_candles_range(self, instrument, granularity, from_time, to_time):
        return self._candles


def _candle(complete: bool, close: float = 100.0):
    return Candle(
        instrument="EUR_USD", granularity="M15", time=datetime(2026, 9, 24, tzinfo=timezone.utc),
        open=close, high=close, low=close, close=close, volume=5, complete=complete,
    )


def _run(coro):
    return asyncio.run(coro)


def test_incomplete_candle_defers_scoring_instead_of_finalizing():
    signal_time = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    now = datetime(2026, 9, 24, 12, 16, tzinfo=timezone.utc)  # just past the 15m horizon
    brokers = {"oanda": _FakeBroker([_candle(complete=False)])}
    price = _run(_expiry_price(brokers, "EUR_USD", HORIZON_LABEL, signal_time, now))
    assert price is None  # deferred, not a premature "final" price from a still-forming candle


def test_complete_candle_is_used_normally():
    signal_time = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    now = datetime(2026, 9, 24, 12, 16, tzinfo=timezone.utc)
    brokers = {"oanda": _FakeBroker([_candle(complete=True, close=101.5)])}
    price = _run(_expiry_price(brokers, "EUR_USD", HORIZON_LABEL, signal_time, now))
    assert price == 101.5

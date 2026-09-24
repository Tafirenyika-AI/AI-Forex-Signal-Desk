"""Unit test for P1-04 (external review, 2026-09-24, T12): the pre-event
baseline for a measured price reaction must be a candle that closed
STRICTLY BEFORE the announcement, not the candle whose window spans it
(whose own close already reflects the post-announcement price).

Run: .venv/Scripts/python.exe -m pytest tests/test_market_reaction.py -v
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

PROJECT_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

from src.broker.base import Candle
from src.models.market_reaction import compute_price_reaction


class _FakeBroker:
    def __init__(self, candles):
        self._candles = candles

    async def get_candles_range(self, pair, granularity, from_time, to_time):
        return self._candles


def _candle(time_, close, open_=None):
    return Candle(
        instrument="EUR_USD", granularity="M5", time=time_,
        open=open_ if open_ is not None else close, high=close, low=close, close=close,
        volume=10, complete=True,
    )


def _run(coro):
    return asyncio.run(coro)


def test_t12_reaction_inside_the_event_start_candle_is_measured_not_zero():
    # Exact T12 scenario: price jumps 100 -> 110 in the event-start candle,
    # then stays flat. event_time falls INSIDE that candle's own 5-minute
    # window (candle starts 2 minutes before event_time).
    event_time = datetime(2026, 9, 24, 12, 30, 0, tzinfo=timezone.utc)
    candles = [
        _candle(event_time - timedelta(minutes=17), 100),  # genuine pre-event baseline
        _candle(event_time - timedelta(minutes=12), 100),
        _candle(event_time - timedelta(minutes=7), 100),
        _candle(event_time - timedelta(minutes=2), 110, open_=100),  # SPANS the event -- jumps 100->110 inside it
        _candle(event_time + timedelta(minutes=3), 110),  # stays flat after
        _candle(event_time + timedelta(minutes=8), 110),
    ]
    broker = _FakeBroker(candles)
    reaction = _run(compute_price_reaction(broker, "USD", event_time))
    assert reaction is not None
    # EUR_USD rose 10% (100->110); USD is the QUOTE currency there, so a
    # rising pair means USD WEAKENED -- the function flips sign for that,
    # correctly reporting -10% for USD's own reaction, not 0.0 (the bug).
    assert abs(reaction - (-0.10)) < 1e-9


def test_no_jump_still_reports_zero_correctly():
    event_time = datetime(2026, 9, 24, 12, 30, 0, tzinfo=timezone.utc)
    candles = [
        _candle(event_time - timedelta(minutes=17), 100),
        _candle(event_time - timedelta(minutes=7), 100),
        _candle(event_time - timedelta(minutes=2), 100),
        _candle(event_time + timedelta(minutes=3), 100),
    ]
    broker = _FakeBroker(candles)
    reaction = _run(compute_price_reaction(broker, "USD", event_time))
    assert reaction == 0.0


def test_reaction_measured_before_this_fix_would_have_been_zero():
    # Negative control: proves the OLD boundary (c.time <= event_time)
    # really did put the event-spanning candle in the baseline bucket.
    event_time = datetime(2026, 9, 24, 12, 30, 0, tzinfo=timezone.utc)
    candles = [
        _candle(event_time - timedelta(minutes=17), 100),
        _candle(event_time - timedelta(minutes=2), 110, open_=100),  # spans the event
        _candle(event_time + timedelta(minutes=3), 110),
    ]
    old_pre_event = [c for c in candles if c.time <= event_time]
    old_post_event = [c for c in candles if c.time > event_time]
    old_start, old_end = old_pre_event[-1].close, old_post_event[-1].close
    assert old_start == 110  # the contaminated candle, wrongly used as the baseline
    assert old_end == 110
    assert (old_end - old_start) / old_start == 0.0  # exactly the reported "zero reaction" bug

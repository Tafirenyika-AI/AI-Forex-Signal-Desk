"""Unit tests for src/backtest/engine.py's intrabar exit walker.

_simulate_exit is the piece every backtested trade's realized return
depends on — a wrong high/low comparison here silently corrupts every
result downstream, so it gets a synthetic-OHLC test per constructed
scenario rather than relying on the walk-forward loop's real data alone.

Run: .venv/Scripts/python.exe -m pytest tests/test_backtest_engine.py -v
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

from src.backtest.engine import _simulate_exit, _simulate_trailing_exit


def _candles(bars: list[dict]) -> pd.DataFrame:
    """bars: list of {"open","high","low","close"} — time/index filled in."""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [
        {"time": base + timedelta(hours=i), **b}
        for i, b in enumerate(bars)
    ]
    return pd.DataFrame(rows)


def test_long_hits_target_before_stop():
    df = _candles([
        {"open": 100, "high": 100, "low": 100, "close": 100},  # entry bar
        {"open": 100, "high": 101, "low": 99, "close": 100.5},  # neither level touched
        {"open": 100.5, "high": 106, "low": 100, "close": 105},  # target (105) hit
    ])
    exit_idx, exit_price, reason = _simulate_exit(
        df, entry_idx=0, direction=1, entry_price=100, stop_distance=3, target_distance=5,
    )
    assert reason == "target"
    assert exit_idx == 2
    assert exit_price == 105


def test_long_hits_stop_before_target():
    df = _candles([
        {"open": 100, "high": 100, "low": 100, "close": 100},
        {"open": 100, "high": 101, "low": 96.5, "close": 97},  # stop (97) hit
        {"open": 97, "high": 110, "low": 97, "close": 108},  # would've hit target, but too late
    ])
    exit_idx, exit_price, reason = _simulate_exit(
        df, entry_idx=0, direction=1, entry_price=100, stop_distance=3, target_distance=5,
    )
    assert reason == "stop"
    assert exit_idx == 1
    assert exit_price == 97


def test_short_hits_target_before_stop():
    df = _candles([
        {"open": 100, "high": 100, "low": 100, "close": 100},
        {"open": 100, "high": 101, "low": 94, "close": 95},  # target (95) hit for a short
    ])
    exit_idx, exit_price, reason = _simulate_exit(
        df, entry_idx=0, direction=-1, entry_price=100, stop_distance=3, target_distance=5,
    )
    assert reason == "target"
    assert exit_idx == 1
    assert exit_price == 95


def test_short_hits_stop_before_target():
    df = _candles([
        {"open": 100, "high": 100, "low": 100, "close": 100},
        {"open": 100, "high": 104, "low": 99, "close": 103},  # stop (103) hit for a short
    ])
    exit_idx, exit_price, reason = _simulate_exit(
        df, entry_idx=0, direction=-1, entry_price=100, stop_distance=3, target_distance=5,
    )
    assert reason == "stop"
    assert exit_price == 103


def test_both_levels_breached_same_bar_assumes_stop_first():
    """OHLC has no intrabar tick order — a bar whose range crosses BOTH the
    stop and target must pick one deterministically. The conservative
    assumption (documented in _simulate_exit's docstring) is that the stop
    hit first."""
    df = _candles([
        {"open": 100, "high": 100, "low": 100, "close": 100},
        {"open": 100, "high": 106, "low": 96, "close": 102},  # both 97 (stop) and 105 (target) crossed
    ])
    exit_idx, exit_price, reason = _simulate_exit(
        df, entry_idx=0, direction=1, entry_price=100, stop_distance=3, target_distance=5,
    )
    assert reason == "stop"
    assert exit_price == 97


def test_runs_out_of_data_closes_at_last_bar_eod():
    df = _candles([
        {"open": 100, "high": 100, "low": 100, "close": 100},
        {"open": 100, "high": 101, "low": 99, "close": 100.5},
        {"open": 100.5, "high": 101, "low": 99.5, "close": 100.2},
    ])
    exit_idx, exit_price, reason = _simulate_exit(
        df, entry_idx=0, direction=1, entry_price=100, stop_distance=10, target_distance=10,
    )
    assert reason == "eod"
    assert exit_idx == 2
    assert exit_price == 100.2


def test_trailing_stop_ratchets_up_and_locks_in_profit_long():
    """A bar's rally ratchets the stop up; a LATER bar's pullback that
    would NOT have hit the original fixed stop (95) does hit the
    ratcheted one (108) — proving the trail actually changes behavior,
    not just carrying the fixed stop along unused."""
    df = _candles([
        {"open": 100, "high": 100, "low": 100, "close": 100, "atr_14": 2},  # entry bar
        {"open": 100, "high": 112, "low": 99, "close": 110, "atr_14": 2},  # rallies; ratchets stop to 108
        {"open": 110, "high": 111, "low": 100, "close": 105, "atr_14": 2},  # pulls back to 100 -> hits 108
    ])
    exit_idx, exit_price, reason = _simulate_trailing_exit(
        df, entry_idx=0, direction=1, entry_price=100, stop_distance=5, target_distance=50, atr_multiplier=1.0,
    )
    assert reason == "stop"
    assert exit_idx == 2
    assert exit_price == 108


def test_trailing_stop_ratchets_down_and_locks_in_profit_short():
    """Mirror of the long case: a short's stop only ever moves down."""
    df = _candles([
        {"open": 100, "high": 100, "low": 100, "close": 100, "atr_14": 2},  # entry bar
        {"open": 100, "high": 101, "low": 88, "close": 90, "atr_14": 2},  # drops; ratchets stop to 92
        {"open": 90, "high": 95, "low": 85, "close": 88, "atr_14": 2},  # bounces to 95 -> hits 92
    ])
    exit_idx, exit_price, reason = _simulate_trailing_exit(
        df, entry_idx=0, direction=-1, entry_price=100, stop_distance=5, target_distance=50, atr_multiplier=1.0,
    )
    assert reason == "stop"
    assert exit_idx == 2
    assert exit_price == 92


def test_trailing_stop_never_loosens_against_the_position():
    """A bar with a huge unfavorable ATR must not widen (loosen) an
    already-ratcheted stop back out — the trail only ever tightens."""
    df = _candles([
        {"open": 100, "high": 100, "low": 100, "close": 100, "atr_14": 1},  # entry bar
        {"open": 100, "high": 112, "low": 99, "close": 110, "atr_14": 1},  # ratchets stop to 109
        # huge ATR here would suggest loosening to 88 (108 - 20) if the
        # ratchet didn't clamp to max(current, candidate) -- must stay 109
        {"open": 110, "high": 111, "low": 110, "close": 108, "atr_14": 20},
        {"open": 108, "high": 109.5, "low": 105, "close": 106, "atr_14": 1},  # hits the still-109 stop
    ])
    exit_idx, exit_price, reason = _simulate_trailing_exit(
        df, entry_idx=0, direction=1, entry_price=100, stop_distance=5, target_distance=50, atr_multiplier=1.0,
    )
    assert reason == "stop"
    assert exit_idx == 3
    assert exit_price == 109


def test_trailing_stop_hit_check_is_causal_not_lookahead():
    """A bar whose OWN huge favorable close would ratchet the stop way up
    must still be checked against the stop level as of the START of that
    bar — a bar can't use its own close to retroactively dodge a stop-out
    its low should have triggered under the pre-ratchet level."""
    df = _candles([
        {"open": 100, "high": 100, "low": 100, "close": 100, "atr_14": 1},  # entry bar
        # low breaches the original stop (95); close (125) would ratchet to
        # 124 if the ratchet were (wrongly) applied before the hit check
        {"open": 100, "high": 130, "low": 90, "close": 125, "atr_14": 1},
    ])
    exit_idx, exit_price, reason = _simulate_trailing_exit(
        df, entry_idx=0, direction=1, entry_price=100, stop_distance=5, target_distance=50, atr_multiplier=1.0,
    )
    assert reason == "stop"
    assert exit_idx == 1
    assert exit_price == 95


def test_max_hold_bars_caps_the_walk_as_timeout():
    df = _candles([
        {"open": 100, "high": 100, "low": 100, "close": 100},
        {"open": 100, "high": 101, "low": 99, "close": 100.5},
        {"open": 100.5, "high": 101, "low": 99.5, "close": 100.2},
        {"open": 100.2, "high": 108, "low": 99, "close": 101},  # would hit target here if reached
    ])
    exit_idx, exit_price, reason = _simulate_exit(
        df, entry_idx=0, direction=1, entry_price=100, stop_distance=10, target_distance=5,
        max_hold_bars=2,
    )
    assert reason == "timeout"
    assert exit_idx == 2
    assert exit_price == 100.2

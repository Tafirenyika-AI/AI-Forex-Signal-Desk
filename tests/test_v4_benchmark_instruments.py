"""V4 Priority 2 (docs/V4_ARCHITECTURE.md Section 4 gap): IWM/GLD/IAU/USO
must be in the backfilled benchmark set alongside the pre-existing SPY +
sector ETFs, so cross-market features (Equity V2 Phase 8) have real small-
cap/gold/oil reference data to draw on.

Run: .venv/Scripts/python.exe -m pytest tests/test_v4_benchmark_instruments.py -v
"""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

from src.equity.relationships import SIC_TO_SECTOR
from src.scripts.backfill_candles import BENCHMARK_INSTRUMENTS


def test_new_v4_benchmarks_are_present():
    for symbol in ("IWM", "GLD", "IAU", "USO"):
        assert symbol in BENCHMARK_INSTRUMENTS


def test_preexisting_benchmarks_are_unaffected():
    assert "SPY" in BENCHMARK_INSTRUMENTS
    for _, (_, etf) in SIC_TO_SECTOR:
        assert etf in BENCHMARK_INSTRUMENTS

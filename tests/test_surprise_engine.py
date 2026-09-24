"""Unit tests for P1-04 (external review, 2026-09-24, T06): matching a
FRED reference-period release to its correct Forex Factory calendar row
must pick the SAME reference period and the SAME unit (m/m, not y/y),
not just whichever FF row happens to publish closest in raw day-count.

Run: .venv/Scripts/python.exe -m pytest tests/test_surprise_engine.py -v
"""
import os
import sys
from datetime import datetime, timezone

PROJECT_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

from src.models.surprise_engine import match_surprises


def _fred_cpi_row(event_time, actual, previous):
    return {
        "event_name": "US CPI (headline, SA)", "currency": "USD",
        "event_time": event_time, "actual": actual, "previous": previous,
    }


def _ff_row(name, event_time, consensus, previous=None):
    return {"event_name": name, "currency": "USD", "event_time": event_time, "consensus": consensus, "previous": previous}


def test_t06_picks_the_same_reference_period_release_not_the_closer_prior_one():
    # Exact T06 scenario: FRED's July CPI is dated July 1 (its reference
    # period start). Forex Factory publishes June's release ~12 days later
    # (mid-July) and July's own release ~43 days later (mid-August) -- the
    # June release is CLOSER in raw day-count but is the WRONG period.
    fred_july = _fred_cpi_row(datetime(2026, 7, 1, tzinfo=timezone.utc), actual=332.8, previous=332.3)
    ff_june_release = _ff_row("CPI m/m", datetime(2026, 7, 13, tzinfo=timezone.utc), consensus=0.1)  # wrong period, closer
    ff_july_release = _ff_row("CPI m/m", datetime(2026, 8, 13, tzinfo=timezone.utc), consensus=0.3)  # correct period, farther

    surprises = match_surprises([fred_july], [ff_june_release, ff_july_release])
    assert len(surprises) == 1
    assert surprises[0].ff_event_time == datetime(2026, 8, 13, tzinfo=timezone.utc)
    assert surprises[0].consensus == 0.3  # the July release's own consensus, not June's


def test_t06_ignores_the_yy_title_and_uses_the_mm_consensus():
    # Same release date, but Forex Factory publishes BOTH "CPI m/m" and
    # "CPI y/y" that day -- FRED's PCT_CHANGE transform computes a MONTHLY
    # change, so only the m/m consensus is the right unit to compare it to.
    fred_july = _fred_cpi_row(datetime(2026, 7, 1, tzinfo=timezone.utc), actual=332.8, previous=332.3)
    ff_mm = _ff_row("CPI m/m", datetime(2026, 8, 13, tzinfo=timezone.utc), consensus=0.1)
    ff_yy = _ff_row("CPI y/y", datetime(2026, 8, 13, tzinfo=timezone.utc), consensus=3.2)  # annual-basis, much larger

    surprises = match_surprises([fred_july], [ff_mm, ff_yy])
    assert len(surprises) == 1
    assert surprises[0].ff_event_name == "CPI m/m"
    assert surprises[0].consensus == 0.1  # not 3.2 -- the y/y figure is the wrong unit


def test_core_cpi_still_excluded_same_as_before():
    fred_july = _fred_cpi_row(datetime(2026, 7, 1, tzinfo=timezone.utc), actual=332.8, previous=332.3)
    ff_core = _ff_row("Core CPI m/m", datetime(2026, 8, 13, tzinfo=timezone.utc), consensus=0.2)
    surprises = match_surprises([fred_july], [ff_core])
    assert surprises == []  # no match -- Core CPI m/m must not match the headline CPI matcher


def test_no_candidate_within_the_lag_window_yields_no_match_not_a_wrong_one():
    fred_july = _fred_cpi_row(datetime(2026, 7, 1, tzinfo=timezone.utc), actual=332.8, previous=332.3)
    ff_too_early = _ff_row("CPI m/m", datetime(2026, 7, 10, tzinfo=timezone.utc), consensus=0.1)  # only 9 days -- below the floor
    surprises = match_surprises([fred_july], [ff_too_early])
    assert surprises == []

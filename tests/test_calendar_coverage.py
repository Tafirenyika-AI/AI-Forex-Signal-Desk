"""Unit tests for P1-04 (external review, 2026-09-24, T03): the risk
governor's calendar-coverage check must require FRESH data for BOTH of a
pair's currencies, not just "a row exists for either one, of any age."

Run: .venv/Scripts/python.exe -m pytest tests/test_calendar_coverage.py -v
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

from src.risk.governor import CALENDAR_FRESHNESS_HOURS, check_calendar_event_risk

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def _event(currency, ingested_at, event_time=None, importance="low", source="ForexFactory"):
    return {
        "currency": currency, "ingested_at": ingested_at,
        "event_time": event_time or NOW + timedelta(days=1),
        "importance": importance, "source": source,
    }


def test_t03_stale_single_currency_row_no_longer_counts_as_coverage():
    # Exact T03 scenario: only a 60-day-old USD row for EUR_USD.
    stale = NOW - timedelta(days=60)
    events = [_event("USD", stale)]
    covers, _ = check_calendar_event_risk(events, "EUR_USD", NOW)
    assert covers is False  # was True before this fix


def test_both_currencies_fresh_covers():
    fresh = NOW - timedelta(hours=1)
    events = [_event("EUR", fresh), _event("USD", fresh)]
    covers, _ = check_calendar_event_risk(events, "EUR_USD", NOW)
    assert covers is True


def test_only_one_of_two_currencies_fresh_does_not_cover():
    # "Both currency policies must be established" -- one fresh currency
    # is not enough for a pair.
    fresh = NOW - timedelta(hours=1)
    events = [_event("EUR", fresh)]  # USD has no row at all
    covers, _ = check_calendar_event_risk(events, "EUR_USD", NOW)
    assert covers is False


def test_exactly_at_the_freshness_boundary_still_covers():
    boundary = NOW - timedelta(hours=CALENDAR_FRESHNESS_HOURS)
    events = [_event("EUR", boundary), _event("USD", boundary)]
    covers, _ = check_calendar_event_risk(events, "EUR_USD", NOW)
    assert covers is True


def test_just_past_the_freshness_boundary_does_not_cover():
    just_stale = NOW - timedelta(hours=CALENDAR_FRESHNESS_HOURS, minutes=1)
    events = [_event("EUR", just_stale), _event("USD", just_stale)]
    covers, _ = check_calendar_event_risk(events, "EUR_USD", NOW)
    assert covers is False


def test_no_rows_at_all_does_not_cover():
    covers, _ = check_calendar_event_risk([], "EUR_USD", NOW)
    assert covers is False


def test_non_forex_instrument_always_covers_regardless_of_calendar_state():
    # Equities/crypto have no currency-calendar concept -- unaffected by this fix.
    covers, upcoming = check_calendar_event_risk([], "NVDA", NOW)
    assert covers is True
    assert upcoming is False


def test_stale_row_from_a_different_source_does_not_count():
    # FRED-sourced rows never establish forward-looking coverage regardless
    # of freshness (see the function's own docstring) -- unaffected by this fix.
    fresh = NOW - timedelta(hours=1)
    events = [_event("EUR", fresh, source="FRED"), _event("USD", fresh, source="FRED")]
    covers, _ = check_calendar_event_risk(events, "EUR_USD", NOW)
    assert covers is False


def test_upcoming_tier1_lockout_unaffected_by_the_freshness_fix():
    # The second return value (lockout detection) is untouched by this
    # change -- a fresh high-importance event within the window still locks out.
    fresh = NOW - timedelta(hours=1)
    events = [
        _event("EUR", fresh, event_time=NOW + timedelta(minutes=10), importance="high"),
        _event("USD", fresh),
    ]
    covers, upcoming = check_calendar_event_risk(events, "EUR_USD", NOW)
    assert covers is True
    assert upcoming is True

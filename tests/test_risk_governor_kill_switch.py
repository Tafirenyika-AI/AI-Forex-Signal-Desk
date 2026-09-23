"""Unit tests for P0-02 (external review, 2026-09-22): the persistent
manual/reconciliation kill-switch latch, weekly-breach persistence for
the remainder of the ISO week, and the per-trade-vs-remaining-daily-
budget clamp.

Uses an isolated in-memory SQLite engine (never the real production DB).

Run: .venv/Scripts/python.exe -m pytest tests/test_risk_governor_kill_switch.py -v
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import create_engine

from src.data.db import metadata
from src.risk import governor


def _fresh_engine():
    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    return engine


def _evaluate(engine, *, account_nav=100_000.0, confidence=0.9, now=None):
    return governor.evaluate(
        engine,
        user_id=1,
        instrument="USD_JPY",
        action="BUY",
        confidence=confidence,
        stop_distance=0.5,
        current_price=150.0,
        current_spread=0.01,
        recent_median_spread=None,
        data_freshness_seconds={},
        account_balance=account_nav,
        account_nav=account_nav,
        open_position_count=1,
        open_positions_usd_direction={},
        component_scores={},
        calendar_covers_currency=True,
        upcoming_tier1_event_within_lockout=False,
        reconciliation_ok=True,
        now=now or datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc),
    )


# --- manual latch persistence ---

def test_manual_kill_switch_blocks_regardless_of_pl():
    engine = _fresh_engine()
    governor.set_manual_kill_switch(engine, 1, "oanda", True, "human pressed stop", "dashboard:test",
                                     datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc))
    decision = _evaluate(engine)
    assert decision.approved is False
    assert decision.reason == "manual kill switch active"


def test_manual_kill_switch_survives_a_simulated_date_rollover():
    engine = _fresh_engine()
    governor.set_manual_kill_switch(engine, 1, "oanda", True, "human pressed stop", "dashboard:test",
                                     datetime(2026, 9, 22, 23, 59, tzinfo=timezone.utc))
    # A fresh evaluate() call "the next day" -- _get_or_init_day_state would
    # create a brand-new risk_state row for the new day, but the manual
    # latch is checked BEFORE that and has no day dimension at all.
    decision = _evaluate(engine, now=datetime(2026, 9, 23, 0, 5, tzinfo=timezone.utc))
    assert decision.approved is False
    assert decision.reason == "manual kill switch active"


def test_manual_kill_switch_clears_when_explicitly_reset():
    engine = _fresh_engine()
    now = datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)
    governor.set_manual_kill_switch(engine, 1, "oanda", True, "stop", "dashboard:test", now)
    governor.set_manual_kill_switch(engine, 1, "oanda", False, None, "dashboard:test", now)
    decision = _evaluate(engine, now=now)
    assert decision.reason != "manual kill switch active"


def test_reconciliation_failure_sets_persistent_manual_latch_not_daily():
    engine = _fresh_engine()
    now = datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)
    decision = governor.evaluate(
        engine, user_id=1, instrument="USD_JPY", action="BUY", confidence=0.9, stop_distance=0.5,
        current_price=150.0, current_spread=0.01, recent_median_spread=None, data_freshness_seconds={},
        account_balance=100_000.0, account_nav=100_000.0, open_position_count=1,
        open_positions_usd_direction={}, component_scores={}, calendar_covers_currency=True,
        upcoming_tier1_event_within_lockout=False, reconciliation_ok=False, now=now,
    )
    assert decision.approved is False
    assert decision.reason == "reconciliation failed"
    manual_state = governor.get_manual_kill_switch(engine, 1, "oanda")
    assert manual_state["active"] is True
    # Survives into "tomorrow" -- the whole point of using the persistent table.
    decision2 = _evaluate(engine, now=datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc))
    assert decision2.approved is False
    assert decision2.reason == "manual kill switch active"


# --- weekly breach persistence ---

def test_weekly_breach_persists_within_the_same_week():
    engine = _fresh_engine()
    monday = datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc)  # 2026-09-21 is a Monday
    # Seed a week_start_balance far above current nav so the weekly check breaches immediately.
    governor._get_or_init_week_state(engine, 1, "oanda", 200_000.0, monday)
    decision = _evaluate(engine, account_nav=100_000.0, now=monday)
    assert decision.approved is False
    assert decision.reason == "weekly loss limit breached"

    # Later the SAME week (Wednesday) -- still blocked, via the persisted flag,
    # even if NAV recovers back above the breach threshold.
    wednesday = datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc)
    decision2 = _evaluate(engine, account_nav=195_000.0, now=wednesday)
    assert decision2.approved is False
    assert decision2.reason == "weekly loss limit breached"


def test_weekly_breach_clears_the_following_week():
    engine = _fresh_engine()
    monday = datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc)
    governor._get_or_init_week_state(engine, 1, "oanda", 200_000.0, monday)
    _evaluate(engine, account_nav=100_000.0, now=monday)  # triggers + persists the breach

    next_monday = datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)
    decision = _evaluate(engine, account_nav=100_000.0, now=next_monday)
    assert decision.reason != "weekly loss limit breached"


# --- per-trade risk vs remaining daily budget ---

def test_single_trade_risk_clamped_to_remaining_daily_budget():
    engine = _fresh_engine()
    now = datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)
    # No prior loss today -> full 1.5% daily budget ($1,500) available.
    # Max-confidence sizing would otherwise reach the 3% ceiling ($3,000).
    decision = _evaluate(engine, account_nav=100_000.0, confidence=1.0, now=now)
    assert decision.approved is True
    assert decision.size_units is not None
    # USD_JPY, stop_distance=0.5, per_unit_usd_risk = 0.5 * (1/150) = 0.003333
    # risk budget clamp: $1,500 -> size <= 1,500 / 0.003333 = 450,000 units
    per_unit_risk = 0.5 * (1 / 150.0)
    assert decision.size_units * per_unit_risk <= 100_000.0 * governor.DAILY_LOSS_LIMIT_PCT + 1e-6


def test_no_remaining_daily_budget_rejects_new_trade():
    engine = _fresh_engine()
    now = datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)
    account_nav = 100_000.0
    # Construct a baseline that leaves remaining daily budget just under
    # one unit's worth of risk (per_unit_usd_risk = 0.5 * (1/150) for
    # USD_JPY) -- close enough to the -1.5% kill-switch breach threshold
    # to leave almost nothing, but not so close it trips that EARLIER gate
    # instead (which fires first, at exactly -1.5%, and would reject with
    # a different, unrelated reason).
    per_unit_usd_risk = 0.5 * (1 / 150.0)
    target_remaining_usd = per_unit_usd_risk * 0.5  # half a unit's worth -> truncates to 0
    loss_so_far_pct = governor.DAILY_LOSS_LIMIT_PCT - target_remaining_usd / account_nav
    baseline = account_nav / (1 - loss_so_far_pct)
    governor._get_or_init_day_state(engine, 1, "oanda", baseline, now)
    decision = _evaluate(engine, account_nav=account_nav, confidence=1.0, now=now)
    assert decision.approved is False
    assert decision.reason == "no remaining daily loss budget"

"""Unit tests for P1-05 (external review, 2026-09-24): analog retrieval
must never pool one user's trade history into another user's "similar
historical situations" summary -- a real cross-user privacy/isolation
leak on this multi-user system, confirmed against real production data
(2 real users) before being fixed.

Uses an isolated in-memory SQLite engine (never the real production DB).

Run: .venv/Scripts/python.exe -m pytest tests/test_analog_retrieval_isolation.py -v
"""
import os
import sys
from datetime import datetime, timezone

PROJECT_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import create_engine, insert

from src.data.db import metadata
from src.data.db import trade_intents as trade_intents_table
from src.data.db import trade_outcomes as trade_outcomes_table
from src.memory.analog_retrieval import MIN_SAMPLES_FOR_INSTRUMENT_MATCH, find_similar_trades


def _fresh_engine():
    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    return engine


def _seed_trade(engine, *, user_id, instrument, regime, action, outcome, realized_pl_usd, horizon="1h",
                 execution_mode="demo"):
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        intent_id = conn.execute(
            insert(trade_intents_table).values(
                user_id=user_id, time=now, instrument=instrument, action=action, confidence=0.9,
                horizon=horizon, regime=regime, stop_distance=0.005, status="EXECUTED", broker="oanda",
            )
        ).inserted_primary_key[0]
        conn.execute(
            insert(trade_outcomes_table).values(
                user_id=user_id, trade_intent_id=intent_id, broker_trade_id=f"t-{intent_id}",
                execution_mode=execution_mode, instrument=instrument, action=action, units=100_000.0,
                entry_price=1.10, exit_price=1.11 if outcome == "WIN" else 1.09,
                realized_pl_usd=realized_pl_usd, closed_at=now, outcome=outcome,
                synced_at=now, broker="oanda",
            )
        )


def test_analog_retrieval_never_pools_another_users_trades():
    # Real bug: user 2's tiny history used to get swamped by user 1's much
    # larger, completely separate account's trade outcomes -- exactly
    # what was confirmed against real production data (2 real users, one
    # with 187,186 trade_intents, the other with 1).
    engine = _fresh_engine()
    # User 1: a large, entirely losing history in this regime/action/instrument.
    for _ in range(MIN_SAMPLES_FOR_INSTRUMENT_MATCH + 5):
        _seed_trade(engine, user_id=1, instrument="EUR_USD", regime="TREND", action="BUY",
                    outcome="LOSS", realized_pl_usd=-50.0)
    # User 2: a small, entirely WINNING history in the exact same bucket.
    for _ in range(MIN_SAMPLES_FOR_INSTRUMENT_MATCH):
        _seed_trade(engine, user_id=2, instrument="EUR_USD", regime="TREND", action="BUY",
                    outcome="WIN", realized_pl_usd=75.0)

    user2_summary = find_similar_trades(engine, user_id=2, instrument="EUR_USD", regime="TREND", action="BUY", horizon="1h", execution_mode="demo")
    assert user2_summary.matched_count == MIN_SAMPLES_FOR_INSTRUMENT_MATCH  # only user 2's own rows
    assert user2_summary.win_rate == 1.0  # not contaminated by user 1's all-losses

    user1_summary = find_similar_trades(engine, user_id=1, instrument="EUR_USD", regime="TREND", action="BUY", horizon="1h", execution_mode="demo")
    assert user1_summary.matched_count == MIN_SAMPLES_FOR_INSTRUMENT_MATCH + 5
    assert user1_summary.win_rate == 0.0  # not contaminated by user 2's all-wins


def test_analog_retrieval_never_pools_a_different_horizon():
    # Real bug: horizon was missing from the match key entirely, so a
    # 15-minute scalp signal's outcome got pooled with a 4-hour swing
    # signal's just because they shared instrument/regime/action.
    engine = _fresh_engine()
    for _ in range(MIN_SAMPLES_FOR_INSTRUMENT_MATCH):
        _seed_trade(engine, user_id=1, instrument="EUR_USD", regime="TREND", action="BUY",
                    outcome="WIN", realized_pl_usd=75.0, horizon="15m", execution_mode="demo")
    for _ in range(MIN_SAMPLES_FOR_INSTRUMENT_MATCH):
        _seed_trade(engine, user_id=1, instrument="EUR_USD", regime="TREND", action="BUY",
                    outcome="LOSS", realized_pl_usd=-40.0, horizon="4h", execution_mode="demo")

    summary_15m = find_similar_trades(engine, user_id=1, instrument="EUR_USD", regime="TREND", action="BUY", horizon="15m", execution_mode="demo")
    assert summary_15m.matched_count == MIN_SAMPLES_FOR_INSTRUMENT_MATCH
    assert summary_15m.win_rate == 1.0  # not diluted by the 4h horizon's all-losses

    summary_4h = find_similar_trades(engine, user_id=1, instrument="EUR_USD", regime="TREND", action="BUY", horizon="4h", execution_mode="demo")
    assert summary_4h.matched_count == MIN_SAMPLES_FOR_INSTRUMENT_MATCH
    assert summary_4h.win_rate == 0.0  # not diluted by the 15m horizon's all-wins


def test_analog_retrieval_never_pools_a_different_execution_mode():
    # Real bug: execution_mode (paper vs demo) was missing from the match
    # key too -- a paper-simulated fill (no real spread/slippage) is a
    # different economic event than a real broker demo-account fill.
    engine = _fresh_engine()
    for _ in range(MIN_SAMPLES_FOR_INSTRUMENT_MATCH):
        _seed_trade(engine, user_id=1, instrument="EUR_USD", regime="TREND", action="BUY",
                    outcome="WIN", realized_pl_usd=75.0, execution_mode="demo")
    for _ in range(MIN_SAMPLES_FOR_INSTRUMENT_MATCH):
        _seed_trade(engine, user_id=1, instrument="EUR_USD", regime="TREND", action="BUY",
                    outcome="LOSS", realized_pl_usd=-40.0, execution_mode="paper")

    demo_summary = find_similar_trades(engine, user_id=1, instrument="EUR_USD", regime="TREND", action="BUY",
                                        horizon="1h", execution_mode="demo")
    assert demo_summary.matched_count == MIN_SAMPLES_FOR_INSTRUMENT_MATCH
    assert demo_summary.win_rate == 1.0  # not diluted by paper mode's all-losses

    paper_summary = find_similar_trades(engine, user_id=1, instrument="EUR_USD", regime="TREND", action="BUY",
                                         horizon="1h", execution_mode="paper")
    assert paper_summary.matched_count == MIN_SAMPLES_FOR_INSTRUMENT_MATCH
    assert paper_summary.win_rate == 0.0  # not diluted by demo mode's all-wins


def test_fallback_across_instruments_also_stays_user_scoped():
    engine = _fresh_engine()
    for _ in range(MIN_SAMPLES_FOR_INSTRUMENT_MATCH):
        _seed_trade(engine, user_id=1, instrument="GBP_USD", regime="RANGE", action="SELL",
                    outcome="WIN", realized_pl_usd=20.0)
    for _ in range(MIN_SAMPLES_FOR_INSTRUMENT_MATCH):
        _seed_trade(engine, user_id=2, instrument="USD_JPY", regime="RANGE", action="SELL",
                    outcome="LOSS", realized_pl_usd=-20.0)

    # User 2 asks about an instrument (EUR_USD) neither user has traded in
    # this regime/action -- falls back to "any instrument" for THEIR OWN
    # data only (USD_JPY), never seeing user 1's GBP_USD rows.
    summary = find_similar_trades(engine, user_id=2, instrument="EUR_USD", regime="RANGE", action="SELL", horizon="1h", execution_mode="demo")
    assert summary.basis == "regime_action_fallback"
    assert summary.matched_count == MIN_SAMPLES_FOR_INSTRUMENT_MATCH
    assert summary.win_rate == 0.0  # user 2's own all-losses, not user 1's all-wins

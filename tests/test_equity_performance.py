"""Unit tests for Equity V2 Phase 15: src/evaluation/equity_performance.py.

Uses an isolated in-memory SQLite engine for trade_outcomes, synthetic
candle/portfolio-history data otherwise. A separate live check (not a
pytest test) was run against the real Alpaca portfolio-history endpoint
and real trade_outcomes data; see docs/EQUITY_V2_IMPLEMENTATION_LOG.md.

Run: .venv/Scripts/python.exe -m pytest tests/test_equity_performance.py -v
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import pytest
from sqlalchemy import create_engine, insert

from src.data.db import metadata
from src.data.db import trade_outcomes as trade_outcomes_table
from src.evaluation.equity_performance import (
    build_performance_report,
    compute_benchmark_return_pct,
    compute_current_unrealized_pl,
    compute_model_attributable_return,
    parse_broker_account_return,
)


@pytest.fixture()
def engine():
    eng = create_engine("sqlite:///:memory:")
    metadata.create_all(eng)
    return eng


# --- parse_broker_account_return ---

def test_parse_broker_account_return_real_captured_shape():
    # Real shape verified live 2026-10-02 against the real Alpaca account.
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    history = {
        "timestamp": [int((start + timedelta(days=i)).timestamp()) for i in range(5)],
        "equity": [100000.0, 100100.0, 100050.0, 100300.0, 100500.0],
        "profit_loss": [0.0, 100.0, 50.0, 300.0, 500.0],
        "profit_loss_pct": [0.0, 0.001, 0.0005, 0.003, 0.005],
        "base_value": 100000.0,
    }
    return_usd, return_pct = parse_broker_account_return(history, start, start + timedelta(days=4))
    assert return_usd == pytest.approx(500.0)
    assert return_pct == pytest.approx(0.005)


def test_parse_broker_account_return_window_not_covered_returns_none():
    history = {"timestamp": [int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())], "equity": [100000.0]}
    return_usd, return_pct = parse_broker_account_return(
        history, datetime(2026, 10, 1, tzinfo=timezone.utc), datetime(2026, 10, 2, tzinfo=timezone.utc),
    )
    assert return_usd is None
    assert return_pct is None


def test_parse_broker_account_return_empty_response_returns_none():
    return_usd, return_pct = parse_broker_account_return({}, datetime.now(timezone.utc), datetime.now(timezone.utc))
    assert return_usd is None
    assert return_pct is None


# --- compute_model_attributable_return ---

def _outcome(**kwargs):
    base = {
        "user_id": 1, "client_order_id": None, "broker_trade_id": "x", "execution_mode": "paper",
        "instrument": "NVDA", "action": "BUY", "units": 10.0, "entry_price": 100.0, "exit_price": 110.0,
        "realized_pl_usd": 100.0, "outcome": "WIN", "closed_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
        "synced_at": datetime(2026, 10, 1, tzinfo=timezone.utc), "broker": "alpaca", "trade_intent_id": None,
    }
    base.update(kwargs)
    return base


def test_model_attributable_vs_unexplained_split(engine):
    with engine.begin() as conn:
        conn.execute(insert(trade_outcomes_table), [
            _outcome(broker_trade_id="a", trade_intent_id=1, realized_pl_usd=200.0),
            _outcome(broker_trade_id="b", trade_intent_id=2, realized_pl_usd=-50.0),
            _outcome(broker_trade_id="c", trade_intent_id=None, realized_pl_usd=999.0),  # unexplained
        ])
    model_pl, n_model, unexplained_pl, n_unexplained = compute_model_attributable_return(
        engine, 1, "alpaca", datetime(2026, 9, 30, tzinfo=timezone.utc), datetime(2026, 10, 2, tzinfo=timezone.utc),
    )
    assert model_pl == pytest.approx(150.0)
    assert n_model == 2
    assert unexplained_pl == pytest.approx(999.0)
    assert n_unexplained == 1


def test_model_attributable_excludes_rows_outside_the_window(engine):
    with engine.begin() as conn:
        conn.execute(insert(trade_outcomes_table), _outcome(
            broker_trade_id="old", trade_intent_id=1, realized_pl_usd=500.0,
            closed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ))
    model_pl, n_model, _, _ = compute_model_attributable_return(
        engine, 1, "alpaca", datetime(2026, 9, 30, tzinfo=timezone.utc), datetime(2026, 10, 2, tzinfo=timezone.utc),
    )
    assert model_pl == 0.0
    assert n_model == 0


def test_model_attributable_excludes_a_different_broker(engine):
    with engine.begin() as conn:
        conn.execute(insert(trade_outcomes_table), _outcome(
            broker_trade_id="oanda1", trade_intent_id=1, realized_pl_usd=500.0, broker="oanda",
        ))
    model_pl, n_model, unexplained_pl, n_unexplained = compute_model_attributable_return(
        engine, 1, "alpaca", datetime(2026, 9, 30, tzinfo=timezone.utc), datetime(2026, 10, 2, tzinfo=timezone.utc),
    )
    assert (model_pl, n_model, unexplained_pl, n_unexplained) == (0.0, 0, 0.0, 0)


# --- compute_current_unrealized_pl ---

def test_current_unrealized_pl_sums_across_positions():
    positions = [{"unrealized_pl": "100.5"}, {"unrealized_pl": "-40.25"}]
    assert compute_current_unrealized_pl(positions) == pytest.approx(60.25)


def test_current_unrealized_pl_empty_positions_is_zero():
    assert compute_current_unrealized_pl([]) == 0.0


# --- compute_benchmark_return_pct ---

def test_benchmark_return_pct_simple_calculation():
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    df = pd.DataFrame({
        "time": [start, start + timedelta(days=1), start + timedelta(days=2)],
        "close": [500.0, 505.0, 510.0],
    })
    result = compute_benchmark_return_pct(df, start, start + timedelta(days=2))
    assert result == pytest.approx(0.02)


def test_benchmark_return_pct_window_not_covered_returns_none():
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    df = pd.DataFrame({"time": [start], "close": [500.0]})
    result = compute_benchmark_return_pct(df, start - timedelta(days=5), start - timedelta(days=4))
    assert result is None


def test_benchmark_return_pct_empty_frame_returns_none():
    assert compute_benchmark_return_pct(pd.DataFrame(), datetime.now(timezone.utc), datetime.now(timezone.utc)) is None


# --- build_performance_report (integration of the above) ---

def test_build_performance_report_never_combines_the_three_numbers(engine):
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    end = start + timedelta(days=2)
    with engine.begin() as conn:
        conn.execute(insert(trade_outcomes_table), [
            _outcome(broker_trade_id="a", trade_intent_id=1, realized_pl_usd=200.0, closed_at=start + timedelta(hours=5)),
            _outcome(broker_trade_id="b", trade_intent_id=None, realized_pl_usd=50.0, closed_at=start + timedelta(hours=6)),
        ])
    portfolio_history = {
        "timestamp": [int((start + timedelta(days=i)).timestamp()) for i in range(3)],
        "equity": [100000.0, 100150.0, 100300.0],
    }
    positions_raw = [{"unrealized_pl": "75.0"}]
    spy_df = pd.DataFrame({"time": [start, end], "close": [500.0, 505.0]})
    qqq_df = pd.DataFrame({"time": [start, end], "close": [400.0, 396.0]})

    report = build_performance_report(
        engine, 1, "alpaca", start, end, portfolio_history, positions_raw, spy_candles=spy_df, qqq_candles=qqq_df,
    )
    # Every figure is independently correct and distinguishable -- no
    # blended "performance" number anywhere.
    assert report.broker_account_return_usd == pytest.approx(300.0)
    assert report.model_attributable_realized_pl_usd == pytest.approx(200.0)
    assert report.unexplained_realized_pl_usd == pytest.approx(50.0)
    assert report.current_unrealized_pl_usd == pytest.approx(75.0)
    assert report.spy_return_pct == pytest.approx(0.01)
    assert report.qqq_return_pct == pytest.approx(-0.01)
    # These four numbers must never silently sum to equal each other --
    # the real test of "never combine ambiguously" is that each is exactly
    # what it claims to be, not an amalgam.
    assert report.broker_account_return_usd != (
        report.model_attributable_realized_pl_usd + report.unexplained_realized_pl_usd + report.current_unrealized_pl_usd
    )

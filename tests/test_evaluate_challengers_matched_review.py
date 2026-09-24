"""Unit test for P1-05 (external review, 2026-09-24): src/scripts/
evaluate_challengers.py's graveyard review must compare champion vs
challenger hit rate over the SAME matched set of trade_intent_ids, not
the champion's unrestricted all-time hit rate.

Demonstrates the real bug directly: a challenger that only ran on a
harder slice of history could look worse than the champion's easy-mode
global average and get wrongly buried, even though it beat the champion
on the exact opportunities they both actually saw.

Run: .venv/Scripts/python.exe -m pytest tests/test_evaluate_challengers_matched_review.py -v
"""
import os
import sys
from datetime import datetime, timezone

PROJECT_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import create_engine, insert, select

from src.data.db import metadata
from src.data.db import signal_evaluations as signal_evaluations_table
from src.data.db import strategy_graveyard as strategy_graveyard_table
from src.scripts.evaluate_challengers import _matched_stats, _review_for_graveyard

CHALLENGER_NAME = "economic_surprise_v1"  # a real name from src/challengers/definitions.py


def _fresh_engine():
    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    return engine


def _seed_eval(engine, *, source, trade_intent_id, hit, move_in_favor=None):
    now = datetime.now(timezone.utc)
    move = move_in_favor if move_in_favor is not None else (0.001 if hit else -0.001)
    with engine.begin() as conn:
        conn.execute(
            insert(signal_evaluations_table).values(
                source=source, trade_intent_id=trade_intent_id, instrument="EUR_USD", horizon="1h",
                action="BUY", signal_time=now, reference_price=1.10,
                expiry_price=1.10 + move, hit=hit, move_in_favor=move, evaluated_at=now,
            )
        )


def test_matched_stats_ignores_unmatched_rows():
    engine = _fresh_engine()
    # Champion has rows both inside and outside the challenger's matched set.
    for i in range(1, 31):
        _seed_eval(engine, source="champion", trade_intent_id=i, hit=(i <= 10))  # 10/30 = 33.3%
    for i in range(31, 61):
        _seed_eval(engine, source="champion", trade_intent_id=i, hit=True)  # all wins, but NOT matched

    matched_ids = set(range(1, 31))
    n, hit_rate, _ = _matched_stats(engine, "champion", matched_ids)
    assert n == 30
    assert abs(hit_rate - (10 / 30)) < 1e-9  # unaffected by the 30 unmatched wins


def test_challenger_not_wrongly_buried_against_champions_easier_global_average(capsys):
    engine = _fresh_engine()
    # Challenger ran on 30 signals (ids 1-30), 18 wins = 60%.
    for i in range(1, 31):
        _seed_eval(engine, source=CHALLENGER_NAME, trade_intent_id=i, hit=(i <= 18))
    # Champion also scored those same 30 (harder slice): 10 wins = 33.3%.
    for i in range(1, 31):
        _seed_eval(engine, source="champion", trade_intent_id=i, hit=(i <= 10))
    # Champion ALSO scored 30 more signals the challenger abstained on
    # (easier slice, all wins) -- pre-fix code would fold these into the
    # comparison baseline; the fix must ignore them entirely.
    for i in range(31, 61):
        _seed_eval(engine, source="champion", trade_intent_id=i, hit=True)

    now = datetime.now(timezone.utc)
    _review_for_graveyard(engine, now)
    output = capsys.readouterr().out

    with engine.connect() as conn:
        buried = conn.execute(
            select(strategy_graveyard_table.c.strategy_name)
            .where(strategy_graveyard_table.c.strategy_name == CHALLENGER_NAME)
        ).first()
    assert buried is None, "challenger beat the champion on the matched population -- must not be buried"
    assert "outperforming" in output
    assert "expectancy" in output  # Fix B: expectancy surfaced alongside hit rate

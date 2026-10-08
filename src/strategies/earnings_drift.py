"""Strategy G — Earnings and Event-Driven Trading (src/strategies/
registry.py's "G").

A direct, honest statistical test of the strategy's own hypothesis
(Bernard & Thomas 1989, post-earnings-announcement drift): does an
earnings surprise's direction predict continued price drift in that
direction over the following days/weeks?

Real data constraint found while building this, disclosed not hidden: no
analyst-consensus-estimate feed exists in this project (a paid data
source, never licensed — see docs/V4_DATA_SOURCES.md). "Surprise" here is
therefore a YEAR-OVER-YEAR EarningsPerShareDiluted change (this quarter's
real, filed EPS vs. the same ticker's real, filed EPS from the nearest
prior-year quarter) — a disclosed, defensible proxy for "beat/missed
expectations," not a claim of matching real analyst consensus.

A second real constraint found: `equity_news`'s own "EARNINGS"-classified
articles (12 total, checked live 2026-10-08) are all multi-ticker market-
roundup pieces ("Wall Street braces for earnings season"), not real
individual-company earnings-reaction reports — unusable as a clean per-
ticker event trigger. Uses `company_fundamentals.filed_at` instead (the
real, point-in-time-correct SEC filing timestamp) as the event anchor —
a few days later than the original earnings press release in practice,
but a REAL, honestly-knowable timestamp, not a fabricated one.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.data.db import candles as candles_table
from src.data.db import company_fundamentals as company_fundamentals_table

EPS_METRIC = "EarningsPerShareDiluted"


@dataclass(frozen=True)
class EarningsDriftResult:
    instrument: str
    holding_days: int
    n: int
    hit_rate: float | None
    z_score: float | None
    mean_move_in_favor: float | None


def _load_eps_events(engine: Engine, ticker: str) -> pd.DataFrame:
    """One row per real reported SINGLE QUARTER: the ORIGINAL filing
    (earliest filed_at for that period_end) — not a later restatement,
    which wouldn't have been knowable to the market at the time of that
    quarter's real earnings reaction.

    Real bug found live 2026-10-08: a Q4/fiscal-year-end period_end is
    shared by BOTH a true single-quarter EPS fact (period_start ~90 days
    before period_end) AND a cumulative year-to-date/annual fact (period_
    start up to 364 days before the same period_end) — this project's own
    data model docstring (src/data/db.py's company_fundamentals table)
    already warns about exactly this XBRL ambiguity. An earlier version of
    this function grouped by period_end alone and could pick up the
    annual figure as if it were a single quarter's EPS (confirmed live:
    MSFT's real quarterly EPS showing as "17.95" — its actual TTM/annual
    figure, not one quarter's). Filtering to a real single-quarter
    duration (80-100 days) before grouping fixes this."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                company_fundamentals_table.c.period_start, company_fundamentals_table.c.period_end,
                company_fundamentals_table.c.filed_at, company_fundamentals_table.c.value,
            )
            .where(company_fundamentals_table.c.ticker == ticker, company_fundamentals_table.c.metric == EPS_METRIC)
            .order_by(company_fundamentals_table.c.period_end, company_fundamentals_table.c.filed_at)
        ).all()
    df = pd.DataFrame(rows, columns=["period_start", "period_end", "filed_at", "value"])
    if df.empty:
        return df
    duration_days = (df["period_end"] - df["period_start"]).dt.days
    df = df[(duration_days >= 80) & (duration_days <= 100)]
    if df.empty:
        return df
    return (
        df.drop(columns="period_start").groupby("period_end", as_index=False).first()
        .sort_values("period_end").reset_index(drop=True)
    )


def _load_closes(engine: Engine, broker: str, instrument: str, granularity: str) -> pd.DataFrame:
    with engine.connect() as conn:
        rows = conn.execute(
            select(candles_table.c.time, candles_table.c.close)
            .where(
                candles_table.c.broker == broker, candles_table.c.instrument == instrument,
                candles_table.c.granularity == granularity, candles_table.c.complete == True,  # noqa: E712
            )
            .order_by(candles_table.c.time)
        ).all()
    return pd.DataFrame(rows, columns=["time", "close"]).drop_duplicates(subset="time")


def evaluate_earnings_drift_hypothesis(
    engine: Engine, broker: str, instrument: str, granularity: str = "H4", holding_days: int = 10,
    yoy_lookback_days: int = 365, yoy_tolerance_days: int = 30,
) -> EarningsDriftResult:
    eps_events = _load_eps_events(engine, instrument)
    if len(eps_events) < 5:
        return EarningsDriftResult(instrument, holding_days, 0, None, None, None)

    closes = _load_closes(engine, broker, instrument, granularity)
    if len(closes) < 10:
        return EarningsDriftResult(instrument, holding_days, 0, None, None, None)
    closes = closes.sort_values("time").reset_index(drop=True)
    closes["log_close"] = np.log(closes["close"])

    directions = []
    event_times = []
    for _, row in eps_events.iterrows():
        target_prior_period = row["period_end"] - timedelta(days=yoy_lookback_days)
        candidates = eps_events[
            (eps_events["period_end"] - target_prior_period).abs() <= timedelta(days=yoy_tolerance_days)
        ]
        candidates = candidates[candidates["period_end"] < row["period_end"]]
        if candidates.empty or pd.isna(row["value"]):
            continue
        prior_value = candidates.iloc[-1]["value"]
        if pd.isna(prior_value) or row["value"] == prior_value:
            continue
        directions.append(1 if row["value"] > prior_value else -1)
        event_times.append(row["filed_at"])

    if not directions:
        return EarningsDriftResult(instrument, holding_days, 0, None, None, None)

    events_df = pd.DataFrame({"event_time": event_times, "direction": directions}).sort_values("event_time")
    fwd_target = events_df[["event_time"]].copy()
    fwd_target["holding_time"] = fwd_target["event_time"] + timedelta(days=holding_days)
    entry = pd.merge_asof(
        events_df.sort_values("event_time"), closes[["time", "log_close"]].rename(columns={"time": "event_time", "log_close": "log_close_entry"}),
        on="event_time", direction="nearest",
    )
    forward = pd.merge_asof(
        fwd_target.sort_values("holding_time"), closes[["time", "log_close"]].rename(columns={"time": "holding_time", "log_close": "log_close_fwd"}),
        on="holding_time", direction="nearest",
    )
    merged = entry.merge(forward[["event_time", "log_close_fwd"]], on="event_time")
    merged["forward_return"] = merged["log_close_fwd"] - merged["log_close_entry"]
    merged = merged[merged["forward_return"] != 0]

    n = len(merged)
    if n == 0:
        return EarningsDriftResult(instrument, holding_days, 0, None, None, None)

    hit = (merged["direction"] * merged["forward_return"]) > 0
    hit_rate = float(hit.mean())
    se = math.sqrt(0.25 / n)
    z_score = (hit_rate - 0.5) / se
    mean_move_in_favor = float((merged["direction"] * merged["forward_return"]).mean())
    return EarningsDriftResult(instrument, holding_days, n, hit_rate, z_score, mean_move_in_favor)

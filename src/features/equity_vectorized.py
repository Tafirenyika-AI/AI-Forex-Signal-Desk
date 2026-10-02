"""Equity V2 Phase 10 support — vectorized (whole-history) equity features,
for training challenger models (Phase 10) and walk-forward validation
(Phase 11) efficiently. src/features/equity_engine.py's
build_equity_feature_vector() is a point lookup (one (ticker, as_of) at a
time, several DB round-trips per call) — correct for a single live
decision, but calling it once per historical bar to build a training set
would be O(bars x rows-scanned-per-call), prohibitively slow across a
ticker's full candle history (thousands of bars x thousands of fundamentals
rows).

Fundamental and news-event features change only at discrete, sparse real
events (a new SEC filing, a new article) — between two such events, their
point-in-time value is CONSTANT. This module exploits that: it calls each
Phase 5/6 function only once per real event (reusing their exact, already-
tested logic, never re-implementing it), then uses pandas' merge_asof to
forward-fill each snapshot onto every bar between events — same point-in-
time guarantee as the per-bar version, computed in a fraction of the time.

Cross-market features (Phase 8) are NOT snapshotted this way — their
rolling-window math is already cheap per call and genuinely time-varying
bar-to-bar (unlike fundamentals/news), so this module calls
compute_cross_market_features directly per bar, reusing Phase 8's exact
tested logic rather than re-deriving a vectorized equivalent. Verified
live to complete in well under a minute for a real ~2,000-bar H1 history
(see docs/EQUITY_V2_IMPLEMENTATION_LOG.md) — a known, disclosed, revisit-
if-it-becomes-a-bottleneck tradeoff, not assumed fast without checking.
"""
from __future__ import annotations

from datetime import timezone

import pandas as pd
from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.data.db import equity_entities as equity_entities_table
from src.data.db import equity_news as equity_news_table
from src.features.equity_cross_market import compute_cross_market_features
from src.features.equity_fundamentals import compute_fundamental_features, load_fundamentals_rows
from src.features.engine import compute_features, load_candles_df
from src.models.regime import classify_regime

FUNDAMENTAL_FEATURE_COLUMNS = [
    "revenue_growth_yoy", "gross_margin", "operating_margin", "net_margin",
    "operating_cash_flow_margin", "leverage_ratio", "eps_diluted",
]
NEWS_EVENT_FEATURE_COLUMNS = ["hours_since_last_news", "has_recent_earnings_event"]
CROSS_MARKET_FEATURE_COLUMNS = [
    "relative_volume", "gap_pct", "vwap_distance", "intraday_volatility_percentile",
    "return_vs_spy", "return_vs_sector_etf",
]

_RECENT_NEWS_WINDOW_HOURS = 24 * 7  # same window src/features/equity_engine.py uses


def _fundamental_columns_asof(ticker_df: pd.DataFrame, fundamentals_rows: list[dict], ticker: str) -> pd.DataFrame:
    """One row per DISTINCT filed_at (not per bar) -- compute_fundamental_
    features' result only ever changes at these points, so this is the
    complete set of times its output can take a new value."""
    filed_ats = sorted({r["filed_at"] for r in fundamentals_rows})
    if not filed_ats:
        empty = pd.DataFrame({"time": pd.Series(dtype="datetime64[ns, UTC]")})
        for col in FUNDAMENTAL_FEATURE_COLUMNS:
            empty[col] = pd.Series(dtype="float64")
        return empty

    snapshots = []
    for filed_at in filed_ats:
        ff = compute_fundamental_features(fundamentals_rows, ticker, filed_at)
        snapshots.append({
            "time": filed_at, "revenue_growth_yoy": ff.revenue_growth_yoy, "gross_margin": ff.gross_margin,
            "operating_margin": ff.operating_margin, "net_margin": ff.net_margin,
            "operating_cash_flow_margin": ff.operating_cash_flow_margin,
            "leverage_ratio": ff.leverage_ratio, "eps_diluted": ff.eps_diluted,
        })
    return pd.DataFrame(snapshots).sort_values("time").reset_index(drop=True)


def _news_columns_asof(news_rows: list[dict]) -> pd.DataFrame:
    """One row per article (not per bar) -- publish_time + event_type is
    all that's needed; hours_since_last_news/has_recent_earnings_event are
    cheap elementwise derivations computed AFTER the merge_asof, against
    each bar's own `time`, not baked in here (baking "hours since" in here
    would freeze it at publish time instead of growing with each bar)."""
    if not news_rows:
        return pd.DataFrame({
            "time": pd.Series(dtype="datetime64[ns, UTC]"),
            "_last_news_publish_time": pd.Series(dtype="datetime64[ns, UTC]"),
            "_last_news_event_type": pd.Series(dtype="object"),
        })
    df = pd.DataFrame(news_rows)[["publish_time", "event_type"]].sort_values("publish_time").reset_index(drop=True)
    df = df.rename(columns={"publish_time": "time"})
    df["_last_news_publish_time"] = df["time"]
    df["_last_news_event_type"] = df["event_type"]
    return df[["time", "_last_news_publish_time", "_last_news_event_type"]]


def build_training_frame(engine: Engine, ticker: str, granularity: str = "H1") -> pd.DataFrame:
    """One row per historical bar, with every feature group's columns
    attached: TECHNICAL (compute_features), MARKET_REGIME (classify_regime),
    FUNDAMENTAL (Phase 5, merge_asof), NEWS_EVENT (Phase 6, merge_asof),
    MICROSTRUCTURE/RELATIVE_STRENGTH (Phase 8, per-bar). Every column is
    point-in-time correct for that bar's own `time` -- the same guarantee
    every upstream phase already enforces, never re-derived loosely here.
    """
    ticker_df = load_candles_df(engine, ticker, granularity)
    if ticker_df.empty:
        return ticker_df

    featured = classify_regime(compute_features(ticker_df))

    fundamentals_rows = load_fundamentals_rows(engine, ticker)
    fundamental_snapshots = _fundamental_columns_asof(ticker_df, fundamentals_rows, ticker)
    # An empty snapshot frame's placeholder `time` dtype can differ in
    # precision (ns vs. us) from the real candle-derived one depending on
    # pandas/platform defaults -- merge_asof requires an exact dtype match
    # on the join key, so this normalizes rather than leaving it to chance.
    fundamental_snapshots["time"] = fundamental_snapshots["time"].astype(featured["time"].dtype)
    featured = pd.merge_asof(
        featured.sort_values("time"), fundamental_snapshots.sort_values("time"),
        on="time", direction="backward",
    )

    # Same exact-comma-token concern as src/features/equity_engine.py's own
    # fix (a naive substring match would false-positive ticker "V" against
    # an "NVDA"-only article): pad both sides with commas so the match is
    # a delimited token, never a substring.
    padded_tickers = "," + equity_news_table.c.tickers + ","
    with engine.connect() as conn:
        news_rows = [dict(r._mapping) for r in conn.execute(
            select(equity_news_table.c.publish_time, equity_news_table.c.event_type)
            .where(padded_tickers.like(f"%,{ticker.upper()},%"))
        )]
    # Same SQLite-vs-Postgres tzinfo round-trip gap already fixed in
    # src/features/equity_fundamentals.py's load_fundamentals_rows -- this
    # module runs its own raw query rather than a shared loader, so the
    # same defensive normalization is repeated here at its own DB boundary.
    for row in news_rows:
        if row["publish_time"].tzinfo is None:
            row["publish_time"] = row["publish_time"].replace(tzinfo=timezone.utc)
    news_snapshots = _news_columns_asof(news_rows)
    news_snapshots["time"] = news_snapshots["time"].astype(featured["time"].dtype)
    featured = pd.merge_asof(
        featured.sort_values("time"), news_snapshots.sort_values("time"),
        on="time", direction="backward",
    )
    featured["hours_since_last_news"] = (
        (featured["time"] - featured["_last_news_publish_time"]).dt.total_seconds() / 3600.0
    )
    featured["has_recent_earnings_event"] = (
        (featured["_last_news_event_type"] == "EARNINGS")
        & (featured["hours_since_last_news"] <= _RECENT_NEWS_WINDOW_HOURS)
    ).astype(float)
    featured = featured.drop(columns=["_last_news_publish_time", "_last_news_event_type"])

    with engine.connect() as conn:
        sector_etf = conn.execute(
            select(equity_entities_table.c.sector_etf).where(equity_entities_table.c.ticker == ticker.upper())
        ).scalar()
    spy_df = load_candles_df(engine, "SPY", granularity)
    sector_etf_df = load_candles_df(engine, sector_etf, granularity) if sector_etf else pd.DataFrame()

    cross_market_cols: dict[str, list] = {col: [] for col in CROSS_MARKET_FEATURE_COLUMNS}
    for _, row in featured.iterrows():
        cm = compute_cross_market_features(
            ticker_df, spy_df if not spy_df.empty else None,
            sector_etf_df if not sector_etf_df.empty else None, ticker, row["time"],
        )
        cross_market_cols["relative_volume"].append(cm.relative_volume)
        cross_market_cols["gap_pct"].append(cm.gap_pct)
        cross_market_cols["vwap_distance"].append(cm.vwap_distance)
        cross_market_cols["intraday_volatility_percentile"].append(cm.volatility_percentile)
        cross_market_cols["return_vs_spy"].append(cm.return_vs_spy)
        cross_market_cols["return_vs_sector_etf"].append(cm.return_vs_sector_etf)
    for col, values in cross_market_cols.items():
        featured[col] = values

    return featured.reset_index(drop=True)

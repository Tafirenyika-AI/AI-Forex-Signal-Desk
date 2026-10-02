"""Equity V2 Phase 9 — unified equity feature engine.

Wires together every feature source built so far (Phase 5 fundamentals,
Phase 6 news, Phase 7 relationships, Phase 8 cross-market, plus the
existing causal technical engine src/features/engine.py and
src/models/regime.py) into ONE feature vector per (ticker, as_of).

The brief's own Phase 9 requirement, implemented literally: every feature
is a FeatureValue, not a bare float — it carries its own `source_
timestamp` (when the underlying data point was actually dated/published)
and `available` flag, so a caller (or a future observability view, Phase
17) can always answer "how fresh was this, and was it even there" per
feature, never just per feature-vector. `available=False` means the
upstream computation returned None (an absence, not a zero) — the exact
"never fabricate missing data" discipline every earlier phase already
established, now surfaced structurally instead of silently.

No feature here is a new computation — this module is pure composition
over already-point-in-time-correct functions (compute_fundamental_
features's `filed_at <= as_of`, compute_cross_market_features's `time <=
as_of`, etc.). The one new point-in-time decision this module makes
itself is MACRO: market_indicators rows are filtered on `ingested_at <=
as_of`, not `observation_date <= as_of` — the economic period a reading
describes is not when this project actually learned it, and the latter
is what a backtest replaying "as of" a historical moment must respect.

PORTFOLIO is the one group this module does NOT compute — a ticker's
current position size, sector concentration, etc. have no "as of a
historical date" meaning from stored history alone; they depend on
whatever portfolio state actually existed at that moment, which this
project has no reliable historical snapshot of yet (Phase 1's
reconciliation `positions_snapshots` only started being written
2026-10-01). `build_equity_feature_vector` accepts an optional
`portfolio_context` dict the CALLER supplies (e.g. from a live run_loop.py
cycle's own broker state) and surfaces it as PORTFOLIO features verbatim
— never synthesized here from nothing.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.data.db import equity_entities as equity_entities_table
from src.data.db import equity_news as equity_news_table
from src.data.db import market_indicators as market_indicators_table
from src.features.engine import TECHNICAL_FEATURE_COLUMNS, compute_features, load_candles_df
from src.features.equity_cross_market import compute_cross_market_features
from src.features.equity_fundamentals import compute_fundamental_features, load_fundamentals_rows
from src.models.regime import classify_regime

MACRO_INDICATORS = ("US10Y", "US2Y")
# Only a news item within this recent a window counts as "a recent
# earnings event" for the has_recent_earnings flag -- long enough to
# cover a typical post-earnings drift window, short enough that month-old
# news isn't still flagged as "recent."
_RECENT_NEWS_WINDOW_HOURS = 24 * 7


def _ensure_utc(value: datetime | None) -> datetime | None:
    """Defensive normalization, not a logic fix: SQLite (used by this
    project's own test suite via an in-memory engine) silently drops
    tzinfo on a DateTime(timezone=True) column round-trip, unlike
    production Postgres which preserves it — confirmed by this module's
    own tests failing with "can't subtract offset-naive and offset-aware
    datetimes" before this guard existed. A naive value read back out is
    assumed UTC (every datetime this project stores is UTC already, see
    every table's own DateTime(timezone=True) columns), never a silent
    wrong-timezone guess."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


@dataclass(frozen=True)
class FeatureValue:
    name: str
    group: str
    value: float | None
    as_of: datetime
    source_timestamp: datetime | None
    available: bool

    @property
    def freshness_seconds(self) -> float | None:
        if self.source_timestamp is None:
            return None
        return (_ensure_utc(self.as_of) - _ensure_utc(self.source_timestamp)).total_seconds()


@dataclass(frozen=True)
class EquityFeatureVector:
    ticker: str
    as_of: datetime
    features: tuple[FeatureValue, ...]

    def as_dict(self) -> dict[str, float | None]:
        return {f.name: f.value for f in self.features}

    def availability_summary(self) -> dict[str, bool]:
        return {f.name: f.available for f in self.features}


def _fv(name: str, group: str, value, as_of: datetime, source_timestamp: datetime | None) -> FeatureValue:
    return FeatureValue(name=name, group=group, value=value, as_of=as_of,
                        source_timestamp=source_timestamp, available=value is not None)


def _latest_row_at_or_before(df: pd.DataFrame, as_of: datetime) -> pd.Series | None:
    eligible = df[df["time"] <= as_of]
    return eligible.iloc[-1] if not eligible.empty else None


def _price_and_technical_features(ticker_df: pd.DataFrame, as_of: datetime) -> list[FeatureValue]:
    if ticker_df.empty:
        return [_fv(col, "PRICE" if col == "log_return_1" else "TECHNICAL", None, as_of, None)
                for col in TECHNICAL_FEATURE_COLUMNS]

    featured = compute_features(ticker_df)
    row = _latest_row_at_or_before(featured, as_of)
    if row is None:
        return [_fv(col, "PRICE" if col == "log_return_1" else "TECHNICAL", None, as_of, None)
                for col in TECHNICAL_FEATURE_COLUMNS]

    source_ts = row["time"]
    out = []
    for col in TECHNICAL_FEATURE_COLUMNS:
        value = row[col]
        group = "PRICE" if col == "log_return_1" else "TECHNICAL"
        out.append(_fv(col, group, None if pd.isna(value) else float(value), as_of, source_ts))
    return out


def _market_regime_features(ticker_df: pd.DataFrame, as_of: datetime) -> list[FeatureValue]:
    if ticker_df.empty:
        return [_fv("vol_percentile", "MARKET_REGIME", None, as_of, None),
                _fv("trend_percentile", "MARKET_REGIME", None, as_of, None)]

    regime_df = classify_regime(compute_features(ticker_df))
    row = _latest_row_at_or_before(regime_df, as_of)
    if row is None:
        return [_fv("vol_percentile", "MARKET_REGIME", None, as_of, None),
                _fv("trend_percentile", "MARKET_REGIME", None, as_of, None)]

    source_ts = row["time"]
    return [
        _fv("vol_percentile", "MARKET_REGIME", None if pd.isna(row["vol_percentile"]) else float(row["vol_percentile"]), as_of, source_ts),
        _fv("trend_percentile", "MARKET_REGIME", None if pd.isna(row["trend_percentile"]) else float(row["trend_percentile"]), as_of, source_ts),
    ]


def _cross_market_features(
    ticker: str, ticker_df: pd.DataFrame, spy_df: pd.DataFrame | None, sector_etf_df: pd.DataFrame | None, as_of: datetime,
) -> list[FeatureValue]:
    cm = compute_cross_market_features(ticker_df, spy_df, sector_etf_df, ticker, pd.Timestamp(as_of))
    source_ts = as_of if not ticker_df.empty else None  # cross-market features are "as of the latest eligible bar," which compute_cross_market_features itself resolves internally
    return [
        _fv("relative_volume", "MICROSTRUCTURE", cm.relative_volume, as_of, source_ts),
        _fv("gap_pct", "MICROSTRUCTURE", cm.gap_pct, as_of, source_ts),
        _fv("vwap_distance", "MICROSTRUCTURE", cm.vwap_distance, as_of, source_ts),
        _fv("intraday_volatility_percentile", "MICROSTRUCTURE", cm.volatility_percentile, as_of, source_ts),
        _fv("return_vs_spy", "RELATIVE_STRENGTH", cm.return_vs_spy, as_of, source_ts),
        _fv("return_vs_sector_etf", "RELATIVE_STRENGTH", cm.return_vs_sector_etf, as_of, source_ts),
    ]


def _fundamental_features(engine: Engine, ticker: str, as_of: datetime) -> list[FeatureValue]:
    rows = load_fundamentals_rows(engine, ticker)
    ff = compute_fundamental_features(rows, ticker, as_of)
    names = [
        ("revenue_growth_yoy", ff.revenue_growth_yoy), ("gross_margin", ff.gross_margin),
        ("operating_margin", ff.operating_margin), ("net_margin", ff.net_margin),
        ("operating_cash_flow_margin", ff.operating_cash_flow_margin),
        ("leverage_ratio", ff.leverage_ratio), ("eps_diluted", ff.eps_diluted),
    ]
    return [_fv(name, "FUNDAMENTAL", value, as_of, ff.revenue_filed_at) for name, value in names]


def _news_event_features(engine: Engine, ticker: str, as_of: datetime) -> list[FeatureValue]:
    # tickers is a comma-joined string (e.g. "AAPL,NVDA,QQQ") -- a naive
    # LIKE '%V%' substring match would false-positive on tickers containing
    # the target as a substring of a DIFFERENT symbol (e.g. ticker "V"
    # matching inside "NVDA"). Padding both sides with commas before
    # matching makes this an exact comma-delimited token match instead.
    padded_tickers = "," + equity_news_table.c.tickers + ","
    stmt = (
        select(equity_news_table)
        .where(padded_tickers.like(f"%,{ticker.upper()},%"), equity_news_table.c.publish_time <= as_of)
        .order_by(equity_news_table.c.publish_time.desc())
        .limit(1)
    )
    with engine.connect() as conn:
        row = conn.execute(stmt).first()

    if row is None:
        return [
            _fv("hours_since_last_news", "NEWS_EVENT", None, as_of, None),
            _fv("has_recent_earnings_event", "NEWS_EVENT", None, as_of, None),
        ]

    publish_time = _ensure_utc(row.publish_time)
    hours_since = (_ensure_utc(as_of) - publish_time).total_seconds() / 3600.0
    has_recent_earnings = 1.0 if (row.event_type == "EARNINGS" and hours_since <= _RECENT_NEWS_WINDOW_HOURS) else 0.0
    return [
        _fv("hours_since_last_news", "NEWS_EVENT", hours_since, as_of, publish_time),
        _fv("has_recent_earnings_event", "NEWS_EVENT", has_recent_earnings, as_of, publish_time),
    ]


def _macro_features(engine: Engine, as_of: datetime) -> list[FeatureValue]:
    out = []
    with engine.connect() as conn:
        for indicator in MACRO_INDICATORS:
            stmt = (
                select(market_indicators_table.c.value, market_indicators_table.c.observation_date)
                .where(market_indicators_table.c.indicator == indicator, market_indicators_table.c.ingested_at <= as_of)
                .order_by(market_indicators_table.c.observation_date.desc())
                .limit(1)
            )
            row = conn.execute(stmt).first()
            if row is None:
                out.append(_fv(indicator, "MACRO", None, as_of, None))
            else:
                out.append(_fv(indicator, "MACRO", float(row.value), as_of, row.observation_date))
    return out


def _portfolio_features(portfolio_context: dict[str, float] | None, as_of: datetime) -> list[FeatureValue]:
    if not portfolio_context:
        return []
    return [_fv(name, "PORTFOLIO", value, as_of, as_of) for name, value in portfolio_context.items()]


def build_equity_feature_vector(
    engine: Engine, ticker: str, as_of: datetime, portfolio_context: dict[str, float] | None = None,
) -> EquityFeatureVector:
    ticker_df = load_candles_df(engine, ticker, "H1")

    with engine.connect() as conn:
        sector_etf = conn.execute(
            select(equity_entities_table.c.sector_etf).where(equity_entities_table.c.ticker == ticker.upper())
        ).scalar()

    spy_df = load_candles_df(engine, "SPY", "H1")
    sector_etf_df = load_candles_df(engine, sector_etf, "H1") if sector_etf else None

    features: list[FeatureValue] = []
    features += _price_and_technical_features(ticker_df, as_of)
    features += _market_regime_features(ticker_df, as_of)
    features += _cross_market_features(ticker, ticker_df, spy_df if not spy_df.empty else None,
                                        sector_etf_df if sector_etf_df is not None and not sector_etf_df.empty else None, as_of)
    features += _fundamental_features(engine, ticker, as_of)
    features += _news_event_features(engine, ticker, as_of)
    features += _macro_features(engine, as_of)
    features += _portfolio_features(portfolio_context, as_of)

    return EquityFeatureVector(ticker=ticker.upper(), as_of=as_of, features=tuple(features))

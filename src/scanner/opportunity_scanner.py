"""AI Trading Desk V4 Priority 4 (brief Section 5 — "Intelligent Opportunity
Scanner"). A thin, transparent ranking layer over Equity V2 Phase 9's
`build_equity_feature_vector` (docs/V4_ARCHITECTURE.md's own Priority 4
recommendation) — no new feature computation, per-factor scoring visible
in every result, never a black-box rank.

Read-only, informational only: this module produces RANKINGS, never a
trade_intent, never a broker call. "Do not automatically trade every
high-ranked opportunity" (the brief's own Section 5 instruction) is
structurally true here, not just a policy — nothing in this module can
place an order.

Split design (same pattern as Strategy F/C/H in src/strategies/): a pure,
directly-testable `rank_from_feature_dicts()` core over already-computed
per-ticker feature dicts, plus a thin `scan_opportunities()` real-data
wrapper that loads real candle availability + calls
build_equity_feature_vector per ticker.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.data.db import candles as candles_table
from src.features.equity_engine import build_equity_feature_vector

# The brief's own Section 5 "initial research candidates" — the default
# universe when none is explicitly passed. Alpaca's crypto symbols use a
# slash; equities/ETFs don't.
DEFAULT_UNIVERSE: tuple[str, ...] = (
    "NVDA", "AMD", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "TSLA",
    "SPY", "QQQ", "IWM", "XLK", "XLF", "XLE", "XLV", "GLD", "IAU", "USO",
    "BTC/USD", "ETH/USD",
)

# (factor_key, feature_name, direction_aware, description)
# direction_aware=True: rank the SIGNED value (bullish ranks high, bearish
# ranks low) -- for factors where sign itself is the meaningful signal.
# direction_aware=False: rank by absolute magnitude -- for factors where
# "how much" is the signal, not which way (volatility/volume/vwap stretch).
_INTRADAY_FACTORS = (
    ("momentum", "log_return_4", True, "short-horizon price momentum"),
    ("relative_strength", "return_vs_spy", True, "return vs. SPY over the lookback window"),
    ("sector_strength", "return_vs_sector_etf", True, "return vs. this ticker's own sector ETF"),
    ("trend_persistence", "trend_percentile", False, "trailing trend strength percentile"),
    ("volatility", "intraday_volatility_percentile", False, "intraday range percentile"),
    ("volume_behavior", "relative_volume", False, "volume vs. its own trailing average"),
    ("vwap_deviation", "vwap_distance", False, "distance from rolling VWAP"),
)
_SWING_FACTORS = (
    ("momentum", "log_return_12", True, "medium-horizon price momentum"),
    ("relative_strength", "return_vs_spy", True, "return vs. SPY over the lookback window"),
    ("sector_strength", "return_vs_sector_etf", True, "return vs. this ticker's own sector ETF"),
    ("trend_persistence", "trend_percentile", False, "trailing trend strength percentile"),
    ("volatility", "vol_percentile", False, "trailing volatility percentile"),
    ("volume_behavior", "relative_volume", False, "volume vs. its own trailing average"),
    ("vwap_deviation", "vwap_distance", False, "distance from rolling VWAP"),
)
FACTOR_SETS = {"intraday": _INTRADAY_FACTORS, "swing": _SWING_FACTORS}

# Disclosed, not silently omitted: the brief's own Section 5 ranking list
# includes "Liquidity," "Bid/ask spread," and "Breakout quality" -- none of
# which has a real, dedicated feature anywhere in this codebase yet (only
# relative_volume as a weak liquidity proxy, already scored above under
# volume_behavior). Fabricating a proxy and labeling it "spread" or
# "breakout quality" would misrepresent what's actually being measured.
UNSCORED_FACTORS_FROM_BRIEF = ("liquidity", "bid_ask_spread", "breakout_quality")


@dataclass(frozen=True)
class ScoredOpportunity:
    ticker: str
    as_of: datetime
    eligible: bool
    ineligibility_reason: str | None
    composite_score: float | None
    factor_scores: dict[str, float]  # factor_key -> percentile rank (0..1), only available factors
    missing_factors: tuple[str, ...]
    reasons: tuple[str, ...]  # transparent, human-readable, one per scored factor
    context: tuple[str, ...]  # informational (regime label, news/earnings), NOT part of composite_score


def rank_from_feature_dicts(
    feature_dicts: dict[str, dict[str, float | None]],
    context_dicts: dict[str, dict[str, object]],
    as_of: datetime,
    timeframe: str = "swing",
) -> list[ScoredOpportunity]:
    """Pure ranking core. `feature_dicts`: ticker -> {feature_name: value},
    e.g. EquityFeatureVector.as_dict()'s own output shape. `context_dicts`:
    ticker -> {"regime": str|None, "has_recent_earnings_event": float|None,
    "hours_since_last_news": float|None} -- informational only."""
    factors = FACTOR_SETS[timeframe]
    tickers = list(feature_dicts.keys())

    # Cross-sectional percentile rank per factor, computed once over every
    # ticker that has a real (non-None) value for it -- a ticker missing a
    # factor is simply excluded from that factor's own ranking, never
    # assigned a fabricated neutral value.
    factor_values: dict[str, dict[str, float]] = {}
    for key, feature_name, direction_aware, _ in factors:
        values = {}
        for ticker in tickers:
            raw = feature_dicts[ticker].get(feature_name)
            if raw is None:
                continue
            values[ticker] = raw if direction_aware else abs(raw)
        factor_values[key] = values

    def _percentile(key: str, ticker: str) -> float | None:
        values = factor_values[key]
        if ticker not in values:
            return None
        this_value = values[ticker]
        all_values = list(values.values())
        return sum(1 for v in all_values if v <= this_value) / len(all_values)

    results: list[ScoredOpportunity] = []
    for ticker in tickers:
        factor_scores: dict[str, float] = {}
        missing: list[str] = []
        reasons: list[str] = []
        for key, feature_name, direction_aware, description in factors:
            raw = feature_dicts[ticker].get(feature_name)
            rank = _percentile(key, ticker)
            if raw is None or rank is None:
                missing.append(key)
                continue
            factor_scores[key] = rank
            n_available = len(factor_values[key])
            sign = "+" if direction_aware and raw >= 0 else ""
            reasons.append(
                f"{key} ({description}): {sign}{raw:.4f} -- percentile rank {rank:.2f} among {n_available} covered tickers"
            )

        composite_score = sum(factor_scores.values()) / len(factor_scores) if factor_scores else None

        ctx = context_dicts.get(ticker, {})
        context_strs = []
        if ctx.get("has_recent_earnings_event"):
            context_strs.append("recent earnings event within the last 7 days (informational, not scored)")

        results.append(ScoredOpportunity(
            ticker=ticker, as_of=as_of, eligible=True, ineligibility_reason=None,
            composite_score=composite_score, factor_scores=factor_scores,
            missing_factors=tuple(missing), reasons=tuple(reasons), context=tuple(context_strs),
        ))

    results.sort(key=lambda r: (r.composite_score is None, -(r.composite_score or 0)))
    return results


def _has_any_candle_history(engine: Engine, instrument: str) -> bool:
    with engine.connect() as conn:
        row = conn.execute(
            select(candles_table.c.id).where(
                candles_table.c.broker == "alpaca", candles_table.c.instrument == instrument,
            ).limit(1)
        ).first()
    return row is not None


def scan_opportunities(
    engine: Engine, as_of: datetime, universe: tuple[str, ...] = DEFAULT_UNIVERSE, timeframe: str = "swing",
) -> list[ScoredOpportunity]:
    """Real-data wrapper. Checks each universe member's candle availability
    live BEFORE scoring it (brief's own "check each asset's availability
    and eligibility before use" instruction) -- an instrument with zero
    backfilled history gets an honest NOT_ELIGIBLE entry, never silently
    dropped or scored with fabricated data."""
    eligible_tickers = []
    ineligible: list[ScoredOpportunity] = []
    for ticker in universe:
        if _has_any_candle_history(engine, ticker):
            eligible_tickers.append(ticker)
        else:
            ineligible.append(ScoredOpportunity(
                ticker=ticker, as_of=as_of, eligible=False,
                ineligibility_reason="no backfilled candle history for this instrument",
                composite_score=None, factor_scores={}, missing_factors=(), reasons=(), context=(),
            ))

    feature_dicts: dict[str, dict[str, float | None]] = {}
    context_dicts: dict[str, dict[str, object]] = {}
    for ticker in eligible_tickers:
        vector = build_equity_feature_vector(engine, ticker, as_of)
        feature_dicts[ticker] = vector.as_dict()
        # The hard regime LABEL (TREND/RANGE/SHOCK/...) isn't itself a
        # feature build_equity_feature_vector exposes (only vol_percentile/
        # trend_percentile are) -- already scored under trend_persistence/
        # volatility above, so not re-derived here with a second candle load.
        context_dicts[ticker] = {
            "has_recent_earnings_event": feature_dicts[ticker].get("has_recent_earnings_event"),
        }

    ranked = rank_from_feature_dicts(feature_dicts, context_dicts, as_of, timeframe)
    return ranked + ineligible

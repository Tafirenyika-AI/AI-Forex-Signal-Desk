"""Equity V2 Phase 5 — fundamental feature engine (growth/margin/cash-flow),
built directly on top of Phase 4's src/equity/sec_edgar.py-sourced
company_fundamentals rows.

Point-in-time discipline, same standard as src/features/engine.py's own
"causal, no future information" rule: every computation here takes an
explicit `as_of` timestamp and only ever considers rows with
`filed_at <= as_of` — a feature computed "as of" a historical date can
never see a filing that hadn't happened yet (Phase 11's walk-forward
validation depends on this holding exactly).

Never fabricate missing data (the brief's own Phase 5 instruction): every
computed ratio is None, not 0 or an imputed value, when an input metric is
unavailable as-of the requested date — FundamentalFeatures.missing_metrics
names exactly which raw inputs were absent, so a downstream caller (Phase
9's feature engine) can make its own informed choice about how to handle
it rather than silently training on a fabricated zero.

Quarterly-vs-cumulative disambiguation: company_fundamentals can hold both
a single-quarter fact and a year-to-date cumulative fact sharing the same
`period_end` (confirmed with real NVDA data in Phase 4) — margin ratios
need the single-quarter figures to mean "this quarter's margin," so
_latest_quarterly_fact prefers the shortest-duration fact among whatever
shares the latest available period_end.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.data.db import company_fundamentals as company_fundamentals_table

_DATETIME_FIELDS = ("period_start", "period_end", "filed_at")


def _ensure_utc(value: datetime | None) -> datetime | None:
    """Defensive normalization, not a logic fix: SQLite (used by this
    project's own test suite via an in-memory engine) silently drops
    tzinfo on a DateTime(timezone=True) column round-trip, unlike
    production Postgres which preserves it — caught by Phase 9's
    equity_engine tests, the first to exercise this function against a
    real (if in-memory) engine rather than pure Python dicts. A naive
    value read back out is assumed UTC (every datetime this project
    stores is UTC already), never a silent wrong-timezone guess."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value

# Revenue can be tagged under either of these in real filings (post-ASC606
# filers commonly switched tags) — treated as one logical "revenue" metric,
# same KEY_METRICS list src/equity/sec_edgar.py ingests both from.
REVENUE_TAGS = ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax")

# A quarterly report spans roughly 90 days; a YTD/cumulative fact for the
# same period_end spans up to ~365. 100 is comfortable headroom above a
# real quarter (even a 13-week reporting quarter) without reaching into
# half-year cumulative territory.
_QUARTER_MAX_DURATION_DAYS = 100

# Year-over-year comparison window: looking for the quarterly fact closest
# to exactly 365 days before the current one, allowing real-world calendar
# drift (reporting dates shift a few days year to year).
_YOY_TARGET_DAYS = 365
_YOY_TOLERANCE_DAYS = 25


@dataclass(frozen=True)
class FundamentalFeatures:
    ticker: str
    as_of: datetime
    revenue: float | None = None
    revenue_period_end: datetime | None = None
    revenue_filed_at: datetime | None = None  # when this value actually became knowable -- proof of no look-ahead
    revenue_growth_yoy: float | None = None
    gross_margin: float | None = None
    operating_margin: float | None = None
    net_margin: float | None = None
    operating_cash_flow_margin: float | None = None
    leverage_ratio: float | None = None  # Liabilities / Assets
    eps_diluted: float | None = None
    missing_metrics: tuple[str, ...] = field(default_factory=tuple)


def load_fundamentals_rows(engine: Engine, ticker: str) -> list[dict]:
    stmt = (
        select(company_fundamentals_table)
        .where(company_fundamentals_table.c.ticker == ticker.upper())
        .order_by(company_fundamentals_table.c.period_end.asc())
    )
    with engine.connect() as conn:
        rows = [dict(row._mapping) for row in conn.execute(stmt)]
    for row in rows:
        for field_name in _DATETIME_FIELDS:
            row[field_name] = _ensure_utc(row[field_name])
    return rows


def _candidates(rows: list[dict], metrics: tuple[str, ...], as_of: datetime) -> list[dict]:
    return [r for r in rows if r["metric"] in metrics and r["filed_at"] <= as_of and r["value"] is not None]


def _latest_quarterly_fact(rows: list[dict], metrics: tuple[str, ...], as_of: datetime) -> dict | None:
    """Among facts knowable as-of `as_of`, restricts to quarter-length
    duration FIRST, then picks the latest period_end among those, then (if
    more than one quarter-length fact ties on that period_end) the most
    recently filed one.

    Real bug caught live against real MSFT data (2026-10-02), not by a
    synthetic test: a fiscal Q4 almost never gets its own standalone XBRL
    fact at all -- a 10-K's income statement reports the FULL FISCAL YEAR,
    not the discrete fourth quarter, so the only fact at a Q4 period_end is
    a ~365-day annual cumulative one. An earlier version of this function
    used duration only as a TIE-BREAK among facts sharing the latest
    period_end, so when that period_end had just one (annual) fact, it was
    accepted as "this quarter's revenue" outright -- MSFT's fiscal Q4
    showed $331.8B "quarterly" revenue, its entire fiscal year's total.
    Filtering to quarter-length duration BEFORE selecting the latest
    period_end means a Q4-only annual fact is correctly treated as not
    having a standalone quarterly figure available (missing_metrics marks
    it), not as a hugely overstated one -- deriving a true Q4 value via
    FY-minus-9-months subtraction is deliberately left as a known gap for
    a later pass, not faked here."""
    candidates = [
        r for r in _candidates(rows, metrics, as_of)
        if (r["period_end"] - r["period_start"]).days <= _QUARTER_MAX_DURATION_DAYS
    ]
    if not candidates:
        return None
    latest_end = max(r["period_end"] for r in candidates)
    at_latest_end = [r for r in candidates if r["period_end"] == latest_end]
    # A restatement of the same quarter (later filed_at) must supersede the
    # value it corrects, not be chosen arbitrarily among ties.
    return max(at_latest_end, key=lambda r: r["filed_at"])


def _prior_year_quarterly_fact(rows: list[dict], metrics: tuple[str, ...], as_of: datetime, reference_end: datetime) -> dict | None:
    """The quarterly fact whose period_end is closest to exactly one year
    before `reference_end`, within a tolerance window -- a real fiscal
    calendar doesn't land on the exact same day/weekday every year."""
    candidates = [
        r for r in _candidates(rows, metrics, as_of)
        if (r["period_end"] - r["period_start"]).days <= _QUARTER_MAX_DURATION_DAYS
    ]
    if not candidates:
        return None
    target = reference_end - timedelta(days=_YOY_TARGET_DAYS)
    best = min(candidates, key=lambda r: abs((r["period_end"] - target).days))
    if abs((best["period_end"] - target).days) > _YOY_TOLERANCE_DAYS:
        return None
    return best


def compute_fundamental_features(rows: list[dict], ticker: str, as_of: datetime) -> FundamentalFeatures:
    """Pure function, no DB/network access -- directly testable against
    synthetic or real captured row sets. `rows` is expected to already be
    this ticker's full company_fundamentals history (any order); filtering
    to what was actually knowable as-of `as_of` happens inside here, not
    trusted to the caller."""
    missing: list[str] = []

    revenue_fact = _latest_quarterly_fact(rows, REVENUE_TAGS, as_of)
    revenue = revenue_fact["value"] if revenue_fact else None
    if revenue_fact is None:
        missing.append("revenue")

    growth_yoy = None
    if revenue_fact is not None:
        prior = _prior_year_quarterly_fact(rows, REVENUE_TAGS, as_of, revenue_fact["period_end"])
        if prior is not None and prior["value"]:
            growth_yoy = (revenue_fact["value"] - prior["value"]) / abs(prior["value"])
        else:
            missing.append("revenue_growth_yoy")  # a real absence (no YoY comparable filed yet), not computed as 0

    def _margin(name: str, metric_tags: tuple[str, ...]) -> float | None:
        if revenue in (None, 0):
            missing.append(name)
            return None
        fact = _latest_quarterly_fact(rows, metric_tags, as_of)
        if fact is None:
            missing.append(name)
            return None
        return fact["value"] / revenue

    gross_margin = _margin("gross_margin", ("GrossProfit",))
    operating_margin = _margin("operating_margin", ("OperatingIncomeLoss",))
    net_margin = _margin("net_margin", ("NetIncomeLoss",))
    ocf_margin = _margin("operating_cash_flow_margin", ("NetCashProvidedByUsedInOperatingActivities",))

    assets_fact = _latest_quarterly_fact(rows, ("Assets",), as_of)
    liabilities_fact = _latest_quarterly_fact(rows, ("Liabilities",), as_of)
    leverage_ratio = None
    if assets_fact is not None and liabilities_fact is not None and assets_fact["value"]:
        leverage_ratio = liabilities_fact["value"] / assets_fact["value"]
    else:
        missing.append("leverage_ratio")

    eps_fact = _latest_quarterly_fact(rows, ("EarningsPerShareDiluted",), as_of)
    eps_diluted = eps_fact["value"] if eps_fact else None
    if eps_fact is None:
        missing.append("eps_diluted")

    return FundamentalFeatures(
        ticker=ticker.upper(),
        as_of=as_of,
        revenue=revenue,
        revenue_period_end=revenue_fact["period_end"] if revenue_fact else None,
        revenue_filed_at=revenue_fact["filed_at"] if revenue_fact else None,
        revenue_growth_yoy=growth_yoy,
        gross_margin=gross_margin,
        operating_margin=operating_margin,
        net_margin=net_margin,
        operating_cash_flow_margin=ocf_margin,
        leverage_ratio=leverage_ratio,
        eps_diluted=eps_diluted,
        missing_metrics=tuple(missing),
    )

"""Economic Surprise Engine (Autonomous Upgrade Spec sec. 7): "Price often
reacts to the difference between what was expected and what actually
occurred... not simply 'good' or 'bad'."

This closes a gap flagged honestly when the two calendar sources were first
built: FRED gives realized actual/previous values but no consensus
(src/macro/fred.py); Forex Factory gives a forward schedule with consensus
forecast but no realized actual (src/macro/forex_factory.py). Neither alone
can compute a true actual-vs-consensus surprise. This module matches them.

Matching is deliberately conservative and rule-based (no fuzzy NLP): a
curated keyword matcher per FRED series name, and a date window (FRED's
`event_time` is the reference period, e.g. "July CPI" dated 2026-07-01;
the real Forex Factory release happens weeks later — this searches forward
up to MAX_MATCH_WINDOW_DAYS and takes the closest FF event with a non-null
consensus). A missed match just means no surprise-vs-consensus signal for
that release, falling back to the existing actual-vs-previous macro model
— never a wrong match forced through.

Only USD has a matcher defined below — that's the only currency with
non-sparse FRED coverage (see src/macro/fred.py's KEY_SERIES docstring).
Extending to EUR/GBP/JPY/CAD/AUD is a natural follow-on once/if those
FRED series prove reliably fresh.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

MAX_MATCH_WINDOW_DAYS = 60


class SeriesTransform(Enum):
    """How to make a FRED value comparable to a Forex Factory consensus
    figure — they're frequently NOT the same unit. Caught live 2026-08-13:
    FRED's CPIAUCSL reports a raw index level (~332.8); Forex Factory's
    "CPI m/m" consensus is a percentage (~0.1) — directly comparing them
    produced a nonsensical >3000% "surprise". Real economic-surprise
    figures are conventionally expressed as a point difference in the
    consensus's own units (e.g. "actual 0.2% vs 0.1% consensus = +0.1pp"),
    never a ratio against a near-zero consensus value."""
    LEVEL = "level"        # FRED already reports the same thing FF forecasts (rates)
    CHANGE = "change"      # FRED reports a level, FF forecasts the period-over-period change
    PCT_CHANGE = "pct_change"  # FRED reports a level, FF forecasts a % change


FRED_SERIES_TRANSFORM = {
    "US CPI (headline, SA)": SeriesTransform.PCT_CHANGE,
    "US Nonfarm Payrolls": SeriesTransform.CHANGE,
    "US Unemployment Rate": SeriesTransform.LEVEL,
    "US Fed Funds Rate": SeriesTransform.LEVEL,
    "US GDP": SeriesTransform.PCT_CHANGE,
}


def _comparable_actual(fred_row: dict, transform: SeriesTransform) -> float:
    actual, previous = fred_row["actual"], fred_row["previous"]
    if transform is SeriesTransform.LEVEL:
        return actual
    if transform is SeriesTransform.CHANGE:
        return actual - previous
    if transform is SeriesTransform.PCT_CHANGE:
        return ((actual - previous) / abs(previous) * 100) if previous else 0.0
    raise ValueError(f"unhandled transform: {transform}")


def _matches_us_cpi(name: str) -> bool:
    n = name.lower()
    return "cpi" in n and "core" not in n


def _matches_us_payrolls(name: str) -> bool:
    n = name.lower()
    return "non-farm" in n or "nonfarm" in n or "payroll" in n


def _matches_us_unemployment(name: str) -> bool:
    return "unemployment rate" in name.lower()


def _matches_us_fed_funds(name: str) -> bool:
    n = name.lower()
    return "federal funds rate" in n or ("fomc" in n and "rate" in n)


def _matches_us_gdp(name: str) -> bool:
    return "gdp" in name.lower()


FRED_TO_FF_MATCHERS: dict[str, callable] = {
    "US CPI (headline, SA)": _matches_us_cpi,
    "US Nonfarm Payrolls": _matches_us_payrolls,
    "US Unemployment Rate": _matches_us_unemployment,
    "US Fed Funds Rate": _matches_us_fed_funds,
    "US GDP": _matches_us_gdp,
}


@dataclass(frozen=True)
class EconomicSurprise:
    currency: str
    fred_event_name: str
    ff_event_name: str
    actual: float  # raw FRED value, as published
    comparable_actual: float  # actual transformed into FF's consensus units
    consensus: float
    previous: float
    surprise_vs_consensus: float  # point difference: comparable_actual - consensus
    surprise_vs_previous: float  # normalized: (actual - previous) / abs(previous)
    fred_event_time: datetime
    ff_event_time: datetime
    ff_importance: str | None


def match_surprises(fred_rows: list[dict], ff_rows: list[dict]) -> list[EconomicSurprise]:
    """fred_rows/ff_rows: economic_events rows already filtered to
    source='FRED' / source='ForexFactory' respectively."""
    results = []
    for fred_row in fred_rows:
        matcher = FRED_TO_FF_MATCHERS.get(fred_row["event_name"])
        if matcher is None or fred_row.get("actual") is None or fred_row.get("previous") is None:
            continue

        candidates = [
            ff for ff in ff_rows
            if ff["currency"] == fred_row["currency"]
            and ff.get("consensus") is not None
            and matcher(ff["event_name"])
            and 0 <= (ff["event_time"] - fred_row["event_time"]).days <= MAX_MATCH_WINDOW_DAYS
        ]
        if not candidates:
            continue
        best = min(candidates, key=lambda ff: abs((ff["event_time"] - fred_row["event_time"]).days))

        transform = FRED_SERIES_TRANSFORM.get(fred_row["event_name"], SeriesTransform.LEVEL)
        actual = fred_row["actual"]
        comparable_actual = _comparable_actual(fred_row, transform)
        consensus = best["consensus"]
        previous = fred_row["previous"]
        surprise_vs_consensus = comparable_actual - consensus  # point difference, same units as consensus
        surprise_vs_previous = (actual - previous) / abs(previous) if previous else 0.0

        results.append(
            EconomicSurprise(
                currency=fred_row["currency"],
                fred_event_name=fred_row["event_name"],
                ff_event_name=best["event_name"],
                actual=actual, comparable_actual=comparable_actual,
                consensus=consensus, previous=previous,
                surprise_vs_consensus=surprise_vs_consensus,
                surprise_vs_previous=surprise_vs_previous,
                fred_event_time=fred_row["event_time"],
                ff_event_time=best["event_time"],
                ff_importance=best.get("importance"),
            )
        )
    return results


def is_meaningful_surprise(surprise: EconomicSurprise) -> bool:
    """"Meaningful" scales with the series' own units — a point-difference
    of 20 is huge for a percentage-based series (CPI) but tiny for a
    thousands-of-jobs series (payrolls' CHANGE transform), so a single
    fixed threshold across series types would be wrong for one or the
    other. Relative-to-consensus with an absolute floor handles both."""
    threshold = max(0.05, abs(surprise.consensus) * 0.15)
    return abs(surprise.surprise_vs_consensus) >= threshold


def detect_reaction_mismatch(surprise: EconomicSurprise, price_reaction_pct: float) -> str | None:
    """Spec sec. 7: 'positive news but currency falls, or negative news but
    currency rises' — evidence of positioning, expectation saturation, or
    an opposing dominant factor. Returns a short label or None if the
    reaction is directionally consistent with the surprise (or either side
    is too small to call a mismatch meaningfully)."""
    if not is_meaningful_surprise(surprise) or abs(price_reaction_pct) < 0.0005:
        return None
    surprise_positive = surprise.surprise_vs_consensus > 0
    reaction_positive = price_reaction_pct > 0
    if surprise_positive != reaction_positive:
        return "mismatch: reaction contradicts consensus surprise direction"
    return None

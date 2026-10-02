"""Unit tests for Equity V2 Phase 5: src/features/equity_fundamentals.py.

compute_fundamental_features is a pure function (no DB/network), so these
tests build synthetic company_fundamentals-shaped row dicts directly --
same style as src/features/engine.py's own tests use synthetic candle
frames. A separate live check (not a pytest test) was run against the
real NVDA rows Phase 4 already wrote to production to confirm this
produces sane real numbers; see docs/EQUITY_V2_IMPLEMENTATION_LOG.md.

Run: .venv/Scripts/python.exe -m pytest tests/test_equity_fundamentals.py -v
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

from src.features.equity_fundamentals import compute_fundamental_features


def _row(metric, period_start, period_end, filed_at, value, ticker="NVDA"):
    return {
        "ticker": ticker, "metric": metric, "value": value,
        "period_start": datetime(*period_start, tzinfo=timezone.utc),
        "period_end": datetime(*period_end, tzinfo=timezone.utc),
        "filed_at": datetime(*filed_at, tzinfo=timezone.utc),
    }


def test_no_rows_at_all_means_every_feature_is_missing_not_fabricated():
    features = compute_fundamental_features([], "NVDA", datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert features.revenue is None
    assert features.gross_margin is None
    assert "revenue" in features.missing_metrics
    assert "gross_margin" in features.missing_metrics


def test_quarterly_and_ytd_revenue_share_period_end_quarterly_is_preferred():
    # Real captured pattern (Phase 4, NVDA): a quarterly fact and a
    # cumulative YTD fact can share the same period_end -- margins must use
    # the quarterly figure, not the inflated cumulative one.
    rows = [
        _row("Revenues", (2026, 1, 26), (2026, 7, 26), (2026, 8, 26), 177_837_000_000),  # YTD (H1)
        _row("Revenues", (2026, 4, 27), (2026, 7, 26), (2026, 8, 26), 96_221_000_000),    # quarterly (Q2)
        _row("GrossProfit", (2026, 4, 27), (2026, 7, 26), (2026, 8, 26), 70_000_000_000),
    ]
    features = compute_fundamental_features(rows, "NVDA", datetime(2026, 9, 1, tzinfo=timezone.utc))
    assert features.revenue == 96_221_000_000  # quarterly, not the 177.8B YTD figure
    assert features.gross_margin == 70_000_000_000 / 96_221_000_000


def test_revenue_growth_yoy_compares_like_for_like_quarters():
    rows = [
        _row("Revenues", (2025, 4, 28), (2025, 7, 27), (2025, 8, 27), 30_000_000_000),  # prior-year Q2
        _row("Revenues", (2026, 4, 27), (2026, 7, 26), (2026, 8, 26), 96_221_000_000),  # this year's Q2
    ]
    features = compute_fundamental_features(rows, "NVDA", datetime(2026, 9, 1, tzinfo=timezone.utc))
    expected = (96_221_000_000 - 30_000_000_000) / 30_000_000_000
    assert abs(features.revenue_growth_yoy - expected) < 1e-9


def test_revenue_growth_yoy_is_none_not_zero_when_no_comparable_prior_quarter_exists():
    # Only one quarter of history exists at all (e.g. a recent IPO) -- there
    # is genuinely no valid year-ago comparison, so growth must be None,
    # never silently computed as 0% or skipped without a trace.
    rows = [_row("Revenues", (2026, 4, 27), (2026, 7, 26), (2026, 8, 26), 96_221_000_000)]
    features = compute_fundamental_features(rows, "NVDA", datetime(2026, 9, 1, tzinfo=timezone.utc))
    assert features.revenue_growth_yoy is None
    assert "revenue_growth_yoy" in features.missing_metrics


def test_point_in_time_a_filing_after_as_of_is_invisible():
    # The real no-look-ahead-bias guarantee: a filing that exists in the
    # table but was filed AFTER the requested as_of date must not affect
    # the computed feature at all -- as if it didn't exist yet.
    rows = [
        _row("Revenues", (2026, 4, 27), (2026, 7, 26), (2026, 8, 26), 96_221_000_000),  # knowable by Sept 1
        _row("Revenues", (2026, 7, 27), (2026, 10, 26), (2026, 11, 20), 100_000_000_000),  # filed too late
    ]
    features = compute_fundamental_features(rows, "NVDA", datetime(2026, 9, 1, tzinfo=timezone.utc))
    assert features.revenue == 96_221_000_000  # the later filing is invisible as-of Sept 1
    assert features.revenue_filed_at == datetime(2026, 8, 26, tzinfo=timezone.utc)

    later_features = compute_fundamental_features(rows, "NVDA", datetime(2026, 12, 1, tzinfo=timezone.utc))
    assert later_features.revenue == 100_000_000_000  # now knowable


def test_a_restatement_with_a_later_filed_at_supersedes_the_original_as_of_that_date():
    # Same period, a later (restated) filed_at value -- the point-in-time
    # feature engine should pick up the most recently filed value for that
    # period, which is also the correct "latest known" state after the
    # restatement. Both rows share period_end, so _latest_quarterly_fact's
    # tie-break (shortest duration) only applies among rows at the latest
    # period_end -- here both candidate periods ARE the latest, so revenue
    # resolves from whichever is actually eligible as-of the query date.
    rows = [
        _row("Revenues", (2026, 4, 27), (2026, 7, 26), (2026, 8, 26), 96_221_000_000),
        _row("Revenues", (2026, 4, 27), (2026, 7, 26), (2026, 11, 1), 96_300_000_000),  # restated later
    ]
    as_of_before_restatement = compute_fundamental_features(rows, "NVDA", datetime(2026, 9, 1, tzinfo=timezone.utc))
    assert as_of_before_restatement.revenue == 96_221_000_000

    as_of_after_restatement = compute_fundamental_features(rows, "NVDA", datetime(2026, 12, 1, tzinfo=timezone.utc))
    assert as_of_after_restatement.revenue == 96_300_000_000  # same period_end, but only the restated filing matters now


def test_margin_is_missing_not_zero_when_revenue_present_but_numerator_absent():
    rows = [_row("Revenues", (2026, 4, 27), (2026, 7, 26), (2026, 8, 26), 96_221_000_000)]
    features = compute_fundamental_features(rows, "NVDA", datetime(2026, 9, 1, tzinfo=timezone.utc))
    assert features.gross_margin is None
    assert "gross_margin" in features.missing_metrics


def test_leverage_ratio_from_instant_metrics():
    rows = [
        _row("Assets", (2026, 7, 26), (2026, 7, 26), (2026, 8, 26), 100_000_000_000),
        _row("Liabilities", (2026, 7, 26), (2026, 7, 26), (2026, 8, 26), 40_000_000_000),
    ]
    features = compute_fundamental_features(rows, "NVDA", datetime(2026, 9, 1, tzinfo=timezone.utc))
    assert features.leverage_ratio == 0.4


def test_fiscal_q4_annual_only_fact_does_not_leak_into_quarterly_revenue():
    # Real bug found live against MSFT production data: a fiscal Q4 often
    # has NO standalone XBRL fact at all -- only the full fiscal year's
    # cumulative figure, tagged in the 10-K. The most recent period_end
    # (fiscal year end) has only this ~365-day annual fact; the function
    # must skip it (not treat $331B as "this quarter's revenue") and fall
    # back to the most recent genuinely quarterly fact instead (Q3).
    rows = [
        _row("Revenues", (2025, 10, 1), (2025, 12, 31), (2026, 1, 28), 81_273_000_000),   # Q2, quarterly
        _row("Revenues", (2026, 1, 1), (2026, 3, 31), (2026, 4, 29), 82_886_000_000),      # Q3, quarterly
        _row("Revenues", (2025, 7, 1), (2026, 6, 30), (2026, 7, 29), 331_839_000_000),     # FY -- annual only, latest period_end
    ]
    features = compute_fundamental_features(rows, "MSFT", datetime(2026, 9, 1, tzinfo=timezone.utc))
    assert features.revenue == 82_886_000_000  # falls back to Q3's real standalone figure
    assert features.revenue_period_end == datetime(2026, 3, 31, tzinfo=timezone.utc)
    assert "revenue" not in features.missing_metrics


def test_revenue_prefers_whichever_of_the_two_revenue_tags_has_data():
    rows = [_row("RevenueFromContractWithCustomerExcludingAssessedTax", (2026, 4, 27), (2026, 7, 26), (2026, 8, 26), 50_000_000_000)]
    features = compute_fundamental_features(rows, "NVDA", datetime(2026, 9, 1, tzinfo=timezone.utc))
    assert features.revenue == 50_000_000_000

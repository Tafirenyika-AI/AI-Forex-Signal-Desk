"""Unit tests for P1-04 (external review, 2026-09-24, T07): a series-aware
sign for economic surprises -- a numerical increase alone is not
uniformly favorable news (a higher-than-expected unemployment RATE is bad
for the currency, unlike CPI/payrolls/GDP/Fed-funds beats).

Run: .venv/Scripts/python.exe -m pytest tests/test_surprise_component.py -v
"""
import os
import sys
from datetime import datetime, timezone

PROJECT_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

from src.models.surprise_component import pair_surprise_score

NOW = datetime(2026, 8, 12, 14, 0, tzinfo=timezone.utc)


def _surprise_row(fred_event_name, currency, surprise_vs_consensus, consensus=4.0, event_time=None):
    return {
        "fred_event_name": fred_event_name, "currency": currency,
        "surprise_vs_consensus": surprise_vs_consensus, "consensus": consensus,
        "ff_event_time": event_time or (NOW - __import__("datetime").timedelta(hours=1)),
    }


def test_t07_unemployment_rate_above_consensus_flips_the_pair_score_via_quote_currency():
    # Exact T07 scenario: unemployment rate 0.2 points above consensus for
    # USD, the QUOTE currency of EUR_USD. Bad news for USD (the fix) means
    # the pair itself scores POSITIVE (EUR strengthens relative to a
    # weaker USD) -- this is the existing, correct quote-currency sign
    # flip composing with the newly-fixed per-series direction. Before
    # this fix, a "beat consensus" unemployment reading was scored as
    # good for USD, which would have made this pair score NEGATIVE instead.
    row = _surprise_row("US Unemployment Rate", "USD", surprise_vs_consensus=0.2, consensus=4.0)
    score, confidence = pair_surprise_score([row], "EUR_USD", NOW)
    assert score > 0


def test_t07_unemployment_rate_as_base_currency_is_unambiguously_negative():
    row = _surprise_row("US Unemployment Rate", "USD", surprise_vs_consensus=0.2, consensus=4.0)
    score, _ = pair_surprise_score([row], "USD_JPY", NOW)  # USD is base -- no sign flip
    assert score < 0  # was positive before this fix


def test_cpi_above_consensus_is_still_positive_unaffected_by_the_fix():
    row = _surprise_row("US CPI (headline, SA)", "USD", surprise_vs_consensus=0.2, consensus=0.1)
    score, _ = pair_surprise_score([row], "USD_JPY", NOW)
    assert score > 0


def test_payrolls_above_consensus_is_still_positive_unaffected_by_the_fix():
    row = _surprise_row("US Nonfarm Payrolls", "USD", surprise_vs_consensus=50.0, consensus=180.0)
    score, _ = pair_surprise_score([row], "USD_JPY", NOW)
    assert score > 0


def test_unemployment_rate_below_consensus_is_positive_for_the_currency():
    # Lower-than-expected unemployment -- a stronger labor market -- should
    # flip to positive under the corrected series-aware sign.
    row = _surprise_row("US Unemployment Rate", "USD", surprise_vs_consensus=-0.2, consensus=4.0)
    score, _ = pair_surprise_score([row], "USD_JPY", NOW)
    assert score > 0

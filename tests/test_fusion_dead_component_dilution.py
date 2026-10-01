"""Unit test for a real bug found live 2026-10-01: src/run_loop.py used to
unconditionally include ComponentView("session", 0.0, 0.0) and
ComponentView("currency_strength", 0.0, 0.0) in every non-forex decision's
component_views.

First-pass fix only excluded those two. Turns out macro/cross_market/news
are EQUALLY forex-only -- pair_macro_score/pair_cross_market_score/
pair_news_score each explicitly return (0.0, 0.0) for any non-forex
instrument (base/quote currency-differential scores with no equity/crypto
equivalent, confirmed by reading those three functions directly and by
checking real logged predictions for MSFT/NVDA/BTC-USD -- all exactly
zero on every real cycle). So for every equity/crypto decision, "price"
was the ONLY component ever carrying real signal -- the other five's
combined nominal weight was diluting combined_confidence far more than
the session/currency_strength-only fix addressed. Quantified against 400
real recent decisions: at the MIN_CONFIDENCE=0.38 gate, 13/400 cleared
under the old math vs. 98/400 under the fully-fixed math -- roughly 7.5x.

This test proves the dilution directly against the real fuse() function
(no run_loop.py plumbing needed -- the bug and its fix are both fully
expressed in what component_views a caller passes in).

Run: .venv/Scripts/python.exe -m pytest tests/test_fusion_dead_component_dilution.py -v
"""
import os
import sys
from datetime import datetime, timezone

PROJECT_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

from src.decision.fusion import ComponentView, fuse

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def test_dead_forex_components_used_to_massively_dilute_equity_confidence():
    price_only = [ComponentView("price", 0.60, 0.60)]
    all_dead_included = price_only + [
        ComponentView("macro", 0.0, 0.0),
        ComponentView("cross_market", 0.0, 0.0),
        ComponentView("news", 0.0, 0.0),
        ComponentView("session", 0.0, 0.0),
        ComponentView("currency_strength", 0.0, 0.0),
    ]

    fixed = fuse(
        instrument="MSFT", horizon="1h", regime="RANGE", component_views=price_only,
        current_price=500.0, atr_14=5.0, data_freshness={}, now=NOW,
    )
    old_buggy = fuse(
        instrument="MSFT", horizon="1h", regime="RANGE", component_views=all_dead_included,
        current_price=500.0, atr_14=5.0, data_freshness={}, now=NOW,
    )

    # Same underlying price signal, same action/score -- only the phantom
    # dead weight differs. With price's own 0.50 base weight against a
    # 1.15 total (RANGE's own regime multipliers aside), the fix should
    # be worth roughly a 2.3x confidence uplift, not a rounding blip.
    assert fixed.action == old_buggy.action
    assert fixed.confidence > old_buggy.confidence
    relative_uplift = fixed.confidence / old_buggy.confidence
    assert 1.5 < relative_uplift < 4.0

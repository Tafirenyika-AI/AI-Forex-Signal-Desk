"""Unit test for a real bug found live 2026-10-01: src/run_loop.py used to
unconditionally include ComponentView("session", 0.0, 0.0) and
ComponentView("currency_strength", 0.0, 0.0) in every non-forex decision's
component_views -- structurally always zero for equities/crypto (see
evaluate_pair's own is_forex gating). A zero-confidence component
contributes nothing to fuse()'s weighted numerator/denominator (correctly
a no-op there), but its nominal weight still counted toward
total_weight, which combined_confidence is scaled against -- silently
dampening every equity/crypto decision's confidence for a purely
structural reason that has nothing to do with real signal quality. Now
that forex is gone, this was happening on every single live decision.

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


def _live_equity_views():
    # Realistic equity-cycle agreement: price/macro/cross_market/news all
    # mildly bullish with real confidence -- no forex-only components.
    return [
        ComponentView("price", 0.30, 0.30),
        ComponentView("macro", 0.20, 0.50),
        ComponentView("cross_market", 0.15, 0.40),
        ComponentView("news", 0.10, 0.20),
    ]


def test_dead_forex_components_used_to_dilute_equity_confidence():
    live_views = _live_equity_views()
    diluted_views = live_views + [
        ComponentView("session", 0.0, 0.0),
        ComponentView("currency_strength", 0.0, 0.0),
    ]

    fixed = fuse(
        instrument="MSFT", horizon="1h", regime="RANGE", component_views=live_views,
        current_price=500.0, atr_14=5.0, data_freshness={}, now=NOW,
    )
    old_buggy = fuse(
        instrument="MSFT", horizon="1h", regime="RANGE", component_views=diluted_views,
        current_price=500.0, atr_14=5.0, data_freshness={}, now=NOW,
    )

    # Same underlying signal, same action/score -- only the phantom dead
    # weight differs. Confidence must be HIGHER once the dead components
    # are correctly excluded (never passed at all), not just unchanged.
    assert fixed.confidence > old_buggy.confidence
    # Quantify: dead weight (session+currency_strength, regime-adjusted)
    # diluted confidence by a real, material amount -- not a rounding blip.
    # (Exact ratio depends on RANGE's own per-component regime multipliers,
    # not a flat 1.0/1.15, so this checks direction + a meaningful bound
    # rather than one precise theoretical number.)
    relative_uplift = fixed.confidence / old_buggy.confidence - 1.0
    assert 0.05 < relative_uplift < 0.30

"""Wraps src/models/surprise_engine.py's matched economic surprises
(P4) into a per-pair component score shaped like macro/cross_market/news
(src/models/{macro,cross_market,news}_model.py) — sign-adjusted for base vs
quote currency exposure, same as those.

This is the concrete deferred signal flagged back in P4: real, live-tested
against FRED+Forex Factory data, but deliberately never wired into the
champion's live decision weight, because validating whether it actually
helps was explicitly left to this priority (Autonomous Upgrade Spec sec.
14-15, champion/challenger). It is only ever used inside a challenger
(src/challengers/), never the champion, until/unless shadow evaluation
proves it out — see src/scripts/evaluate_challengers.py.
"""
from __future__ import annotations

from datetime import datetime

# Same lookback as market_reaction.py's own "still relevant" window for a
# single release — a CPI surprise from 6 hours ago still says something
# about USD; one from a week ago has long since been priced in.
SURPRISE_LOOKBACK_HOURS = 24


def pair_surprise_score(surprise_rows: list[dict], pair: str, now: datetime) -> tuple[float, float]:
    """(score, confidence). Normalizes each surprise using the exact same
    scale-aware threshold as surprise_engine.is_meaningful_surprise()
    (max(0.05, |consensus| * 0.15)) rather than a new fixed magic number —
    a surprise right at that "meaningful" boundary scores +-1, bigger
    surprises saturate there too (clipped, not unbounded)."""
    base, quote = pair.split("_")
    contributions = []
    for row in surprise_rows:
        if row["currency"] not in (base, quote):
            continue
        age_hours = (now - row["ff_event_time"]).total_seconds() / 3600
        if age_hours < 0 or age_hours > SURPRISE_LOOKBACK_HOURS:
            continue

        threshold = max(0.05, abs(row["consensus"]) * 0.15)
        score = max(-1.0, min(1.0, row["surprise_vs_consensus"] / threshold))
        if row["currency"] == quote:
            score = -score

        recency_weight = 1.0 - (age_hours / SURPRISE_LOOKBACK_HOURS)
        contributions.append((score, recency_weight))

    if not contributions:
        return 0.0, 0.0

    total_weight = sum(w for _, w in contributions)
    avg_score = sum(s * w for s, w in contributions) / total_weight if total_weight else 0.0
    # More corroborating releases (up to 2) and more recency both raise
    # confidence — same "more agreement = more trust" shape as the other
    # components' confidence formulas.
    confidence = min(1.0, len(contributions) / 2) * (total_weight / len(contributions))
    return avg_score, confidence

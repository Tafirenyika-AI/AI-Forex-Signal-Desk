"""V4 Priority 6 (brief Section 14 gap, docs/V4_ARCHITECTURE.md Priority 6):
corporate-action (stock split) DETECTION — not full historical back-
adjustment, which would need a real, licensed corporate-actions calendar
this project doesn't have (disclosed, same "no fabricated data" standard
as everywhere else in this codebase).

Real risk this addresses: this project's own `src/broker/alpaca.py` never
passes Alpaca's `adjustment` parameter on bar requests, so all backfilled
history is RAW (split/dividend-unadjusted) by default. A future stock
split in any tracked instrument would silently corrupt every technical/
regime/momentum feature computed across that boundary (a single raw bar
showing, e.g., a fake "-90%" return for a real 10-for-1 split) with no
guard anywhere in this codebase today.

Checked live, 2026-10-08: scanned every currently-backfilled instrument's
real candle history for single-bar moves exceeding 15% — found exactly
one (NVDA, 2025-04-09, +15.9%, the real April 2025 tariff-rally session,
not a split) and confirmed it does NOT match any common split ratio. No
corporate-action corruption currently exists in this project's real data
— this module is a forward-looking safeguard, not a fix for a live bug.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pandas as pd

# ratio = close[t] / close[t-1] for a real N-for-M split, within tolerance.
# A forward "N-for-1" split means each holder gets N shares for 1 -- the
# PRICE DIVIDES by N (ratio = 1/N). A reverse "1-for-N" split means N old
# shares become 1 -- price MULTIPLIES by N (ratio = N). Found and fixed a
# real labeling bug in this dict before it ever shipped (caught by its own
# test, not live): an earlier version had "2-for-1" mapped to 2.0, backwards
# from what that label actually means.
COMMON_SPLIT_RATIOS: dict[str, float] = {
    "2-for-1": 0.5, "3-for-1": 1 / 3, "3-for-2": 2 / 3, "4-for-1": 0.25,
    "5-for-1": 0.2, "7-for-1": 1 / 7, "10-for-1": 0.1, "20-for-1": 0.05,
    "1-for-2": 2.0, "1-for-3": 3.0, "1-for-4": 4.0, "1-for-5": 5.0,
    "1-for-10": 10.0, "1-for-20": 20.0,
}


@dataclass(frozen=True)
class SplitCandidate:
    instrument: str
    time: datetime
    prior_close: float
    close: float
    ratio: float
    likely_split: str  # e.g. "2-for-1" -- the nearest common ratio this matched


def detect_likely_stock_splits(
    instrument: str, times: pd.Series, closes: pd.Series,
    move_threshold: float = 0.15, ratio_tolerance: float = 0.03,
) -> list[SplitCandidate]:
    """Pure function over an already-loaded (time, close) series, sorted
    ascending. Flags a bar only if BOTH (a) the single-bar move exceeds
    `move_threshold` (a real, large move — most aren't splits) AND (b) the
    ratio is close to a real, common split ratio (within `ratio_tolerance`)
    — a large move alone (a real rally/selloff/earnings reaction) is NOT
    enough; a huge number of legitimate big moves exist and must not be
    misflagged as corporate actions."""
    closes = closes.reset_index(drop=True)
    times = times.reset_index(drop=True)
    candidates = []
    for i in range(1, len(closes)):
        prior = closes.iloc[i - 1]
        current = closes.iloc[i]
        if prior == 0:
            continue
        move = abs(current / prior - 1)
        if move < move_threshold:
            continue
        ratio = current / prior
        for label, common_ratio in COMMON_SPLIT_RATIOS.items():
            if abs(ratio - common_ratio) / common_ratio <= ratio_tolerance:
                candidates.append(SplitCandidate(
                    instrument=instrument, time=times.iloc[i], prior_close=float(prior),
                    close=float(current), ratio=float(ratio), likely_split=label,
                ))
                break
    return candidates

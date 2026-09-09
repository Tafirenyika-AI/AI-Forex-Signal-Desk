"""Trailing-stop decision logic (Phase D2). Pure — takes no broker, holds
no state, does not call an API. Same ratchet math backtested and verified
in src/backtest/engine.py's _simulate_trailing_exit (Phase D1: drawdown and
payoff ratio improved in every one of 5 real pairs, hit rate dropped, net
return was genuinely mixed) — this is that same logic, one live cycle at a
time instead of one historical bar at a time.

The caller (src/run_loop.py's per-cycle step) is responsible for treating
the broker as the source of truth for the CURRENT stop price — there is no
local ledger here to drift out of sync with it (see the plan this was
built from for why that's a deliberate choice, not an oversight).
"""
from __future__ import annotations

from src.decision.fusion import ATR_STOP_MULTIPLIER

# How much of one ATR a candidate stop must clear beyond the current stop
# before it's worth a broker API call — without this, a live cycle every
# ~5 minutes would fire a modify_stop_loss call for every negligible tick,
# pure API chatter for no real protective benefit.
MIN_MOVE_ATR_FRACTION = 0.25


def compute_new_stop(
    direction: int,  # 1 = long, -1 = short
    current_price: float,
    current_stop_price: float,
    atr_14: float,
    atr_multiplier: float = ATR_STOP_MULTIPLIER,
    min_move_atr_fraction: float = MIN_MOVE_ATR_FRACTION,
) -> float | None:
    """Returns a new stop price to move to, or None if no update is
    warranted this cycle (either the ratchet hasn't moved enough yet, or
    the ATR reading is unusable). Never returns a price that would loosen
    the stop — a long's stop only ever rises, a short's only ever falls,
    same invariant _simulate_trailing_exit enforces in the backtest."""
    if atr_14 <= 0:
        return None  # unusable ATR reading — no opinion, not a false "no move"

    min_move = min_move_atr_fraction * atr_14

    if direction == 1:
        candidate = current_price - atr_multiplier * atr_14
        if candidate <= current_stop_price + min_move:
            return None
        return candidate
    else:
        candidate = current_price + atr_multiplier * atr_14
        if candidate >= current_stop_price - min_move:
            return None
        return candidate

"""R-multiple: realized P&L expressed as a multiple of the dollar risk taken
at entry (Autonomous Upgrade Spec sec. 13: "Store outcome in R-multiples and
currency value"). Reconstructed after the fact from trade_intents.stop_distance
and trade_outcomes.units/entry_price — the risk governor (src/risk/governor.py)
doesn't persist the exact risk_amount_usd it computed at sizing time, only the
resulting size_units, so R is derived rather than looked up directly.
"""
from __future__ import annotations

from src.risk.governor import usd_value_per_unit


def compute_r_multiple(
    instrument: str,
    entry_price: float,
    stop_distance: float | None,
    units: int,
    realized_pl_usd: float,
) -> float | None:
    if not stop_distance or stop_distance <= 0 or units == 0:
        return None
    try:
        per_unit_usd_risk = stop_distance * usd_value_per_unit(instrument, entry_price)
    except ValueError:
        return None
    dollar_risk = per_unit_usd_risk * abs(units)
    if dollar_risk <= 0:
        return None
    return realized_pl_usd / dollar_risk

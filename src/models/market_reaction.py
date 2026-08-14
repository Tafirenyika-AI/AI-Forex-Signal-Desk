"""Measures actual price movement around a scheduled release (Autonomous
Upgrade Spec sec. 7: "Measure immediate 1m/5m/15m/60m market reaction...
Detect reaction mismatch").

Uses M5 candles (5-minute), not the spec's ideal 1m resolution — this
system has no continuous tick/1m data collection running (that would need
a persistent streaming service, not a periodic scheduled job; see
project_ai_forex_system memory on why every other data source here is
polled, not streamed). M5 is a real, honest measurement at a coarser
resolution, not a fabricated finer one.
"""
from __future__ import annotations

from datetime import timedelta

from src.broker.oanda import OandaBroker

# Any pair containing the currency works to measure its reaction — these
# are simply the most liquid v1 pair for each.
REPRESENTATIVE_PAIR = {
    "USD": "EUR_USD", "EUR": "EUR_USD", "GBP": "GBP_USD",
    "JPY": "USD_JPY", "CAD": "USD_CAD", "AUD": "AUD_USD",
}

PRE_EVENT_BUFFER_MINUTES = 15


async def compute_price_reaction(
    broker: OandaBroker, currency: str, event_time, window_minutes: int = 60
) -> float | None:
    """Returns % price change attributable to `currency` strengthening
    (positive) or weakening (negative) in the window after event_time, or
    None if no representative pair is defined or candle data is missing."""
    pair = REPRESENTATIVE_PAIR.get(currency)
    if not pair:
        return None

    from_time = event_time - timedelta(minutes=PRE_EVENT_BUFFER_MINUTES)
    to_time = event_time + timedelta(minutes=window_minutes)
    candles = await broker.get_candles_range(pair, "M5", from_time, to_time)
    if len(candles) < 2:
        return None

    pre_event = [c for c in candles if c.time <= event_time]
    post_event = [c for c in candles if c.time > event_time]
    if not pre_event or not post_event:
        return None

    start_price = pre_event[-1].close
    end_price = post_event[-1].close
    pct_change = (end_price - start_price) / start_price

    base, quote = pair.split("_")
    if currency == quote:
        pct_change = -pct_change  # flip so positive always means THIS currency strengthened
    return pct_change

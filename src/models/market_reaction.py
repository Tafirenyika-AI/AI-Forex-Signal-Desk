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
# Matches the "M5" granularity requested from get_candles_range below --
# see compute_price_reaction's own fix note for why this matters.
CANDLE_GRANULARITY_MINUTES = 5


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

    # Real bug found 2026-09-24 (external review, P1-04, T12): `c.time` is a
    # candle's OPEN/start timestamp, not its close -- a candle starting
    # just before event_time can still SPAN the announcement (its own
    # 5-minute window covers the moment the release happens), so its
    # CLOSE already reflects the post-announcement price. Using `c.time <=
    # event_time` put that contaminated candle in the "pre-event baseline"
    # instead of the "reaction" bucket -- a real jump measured as its own
    # baseline, always reporting a reaction of exactly zero the instant a
    # move happens inside the event-start candle itself, no matter how big
    # the real move was. Only a candle whose ENTIRE window closed strictly
    # before the event is a genuine baseline; the event-start candle (and
    # everything after) belongs in the reaction measurement.
    candle_duration = timedelta(minutes=CANDLE_GRANULARITY_MINUTES)
    pre_event = [c for c in candles if c.time + candle_duration <= event_time]
    post_event = [c for c in candles if c.time + candle_duration > event_time]
    if not pre_event or not post_event:
        return None

    start_price = pre_event[-1].close
    end_price = post_event[-1].close
    pct_change = (end_price - start_price) / start_price

    base, quote = pair.split("_")
    if currency == quote:
        pct_change = -pct_change  # flip so positive always means THIS currency strengthened
    return pct_change

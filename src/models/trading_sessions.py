"""Trading-session awareness (user-requested 2026-08-13): forex trades 24/5
across four overlapping regional sessions with genuinely different
volatility/liquidity/behavioral character — Sydney and Tokyo are typically
quiet and range-building, London brings the day's first real volume and
often sets a directional range, and New York either continues London's
move or corrects/reverses it, especially in the first hour after NY opens.
None of that is in this system's regime detector (src/models/regime.py,
which is purely price-derived and instrument-relative) — this is a
genuinely new, session-clock-derived input.

Session hours are computed from each market's real local business day via
`zoneinfo`, not a fixed UTC lookup table. That matters: London and New York
shift their UTC offset twice a year (US/UK daylight saving, on different
calendar dates from each other), so a hardcoded "London = 08:00-17:00 UTC"
table would silently drift wrong for a few weeks every spring and autumn.
Tokyo and Sydney don't observe daylight saving (Sydney only shifts because
*Australia's* southern-hemisphere DST is on the opposite half of the year),
so this only actually matters for two of the four — but computing all four
the same way is simpler than special-casing which ones need it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

SESSION_TIMEZONES: dict[str, ZoneInfo] = {
    "Sydney": ZoneInfo("Australia/Sydney"),
    "Tokyo": ZoneInfo("Asia/Tokyo"),
    "London": ZoneInfo("Europe/London"),
    "New_York": ZoneInfo("America/New_York"),
}

# Approximates each session to that market's real business day — the same
# convention most broker "session clock" widgets use, which is what
# produces the commonly-cited ~08:00-17:00 UTC London / ~13:00-22:00 UTC
# New York windows, but (via zoneinfo above) correctly shifts with each
# zone's own real DST rules instead of drifting wrong twice a year.
SESSION_OPEN_LOCAL_HOUR = 8.0
SESSION_CLOSE_LOCAL_HOUR = 17.0

# A session that opened within the last N minutes, or closes within the
# next N, is flagged as a "transition window" — self-relative to each
# session's own hours, not a fixed clock time tied to one specific session.
# 45 minutes is roughly how long the characteristic post-open volatility
# spike / early-session directional move tends to play out before a
# session settles into its own range.
TRANSITION_WINDOW_MINUTES = 45

# Standard FX market convention: closed Friday 22:00 UTC through Sunday
# 22:00 UTC (New York's Friday close through Sydney's Sunday open,
# expressed directly in UTC since that boundary is the actual market
# convention, not derived from any one session's local clock).
_WEEKEND_CLOSE_START_UTC_HOUR = 22
_WEEKEND_REOPEN_UTC_HOUR = 22


@dataclass(frozen=True)
class SessionState:
    now_utc: datetime
    active_sessions: list[str]
    just_opened: list[str]     # active AND opened within TRANSITION_WINDOW_MINUTES
    closing_soon: list[str]    # active AND closes within TRANSITION_WINDOW_MINUTES
    overlap: bool              # 2+ sessions active right now (highest-liquidity windows)
    market_closed: bool        # real weekend closure — no session is "active" and it isn't a data gap


def _is_market_closed(now_utc: datetime) -> bool:
    weekday = now_utc.weekday()  # Mon=0 .. Sun=6
    if weekday == 5:  # Saturday: always closed
        return True
    if weekday == 4 and now_utc.hour >= _WEEKEND_CLOSE_START_UTC_HOUR:  # Friday after close
        return True
    if weekday == 6 and now_utc.hour < _WEEKEND_REOPEN_UTC_HOUR:  # Sunday before reopen
        return True
    return False


def current_session_state(now_utc: datetime) -> SessionState:
    market_closed = _is_market_closed(now_utc)
    active, just_opened, closing_soon = [], [], []

    if not market_closed:
        for name, tz in SESSION_TIMEZONES.items():
            local = now_utc.astimezone(tz)
            if local.weekday() >= 5:  # this session's own local calendar day is a weekend
                continue
            hour_frac = local.hour + local.minute / 60
            if SESSION_OPEN_LOCAL_HOUR <= hour_frac < SESSION_CLOSE_LOCAL_HOUR:
                active.append(name)
                minutes_since_open = (hour_frac - SESSION_OPEN_LOCAL_HOUR) * 60
                minutes_until_close = (SESSION_CLOSE_LOCAL_HOUR - hour_frac) * 60
                if minutes_since_open <= TRANSITION_WINDOW_MINUTES:
                    just_opened.append(name)
                if minutes_until_close <= TRANSITION_WINDOW_MINUTES:
                    closing_soon.append(name)

    return SessionState(
        now_utc=now_utc, active_sessions=active, just_opened=just_opened,
        closing_soon=closing_soon, overlap=len(active) >= 2, market_closed=market_closed,
    )


def previous_session(session_name: str) -> str:
    """The session whose close typically hands off to this one — used to
    look up "what did the prior session do" (e.g. New_York's handoff is
    London; London's is Tokyo). Real forex session order, not alphabetical."""
    order = ["Sydney", "Tokyo", "London", "New_York"]
    idx = order.index(session_name)
    return order[idx - 1]  # wraps New_York -> ... -> Sydney correctly via Python's -1 indexing


def _session_window_for_local_date(session_name: str, local_date) -> tuple[datetime, datetime]:
    tz = SESSION_TIMEZONES[session_name]
    open_local = datetime.combine(local_date, time(hour=int(SESSION_OPEN_LOCAL_HOUR)), tzinfo=tz)
    close_local = datetime.combine(local_date, time(hour=int(SESSION_CLOSE_LOCAL_HOUR)), tzinfo=tz)
    return open_local.astimezone(timezone.utc), close_local.astimezone(timezone.utc)


def most_recent_session_window(session_name: str, now_utc: datetime) -> tuple[datetime, datetime]:
    """The most recently completed (or currently in-progress) window for
    this session, walking back over weekends when needed — this is what
    lets "what did London just do" mean something concrete: a real
    (open_utc, close_utc) pair to fetch real candle data for."""
    tz = SESSION_TIMEZONES[session_name]
    local_now = now_utc.astimezone(tz)
    hour_frac = local_now.hour + local_now.minute / 60
    candidate_date = local_now.date()

    # Today's occurrence hasn't started yet (or today is a weekend day for
    # this zone) — the most recent real occurrence is an earlier weekday.
    if hour_frac < SESSION_OPEN_LOCAL_HOUR or local_now.weekday() >= 5:
        candidate_date -= timedelta(days=1)
    while candidate_date.weekday() >= 5:
        candidate_date -= timedelta(days=1)

    return _session_window_for_local_date(session_name, candidate_date)


@dataclass(frozen=True)
class PriorSessionRange:
    session_name: str
    open_time: datetime
    close_time: datetime
    high: float
    low: float
    open_price: float
    close_price: float
    range_size: float    # high - low, in the instrument's own price units
    net_direction: float  # +1.0 bullish, -1.0 bearish, 0.0 flat — sign only


async def compute_prior_session_range(broker, instrument: str, session_name: str, now_utc: datetime) -> PriorSessionRange | None:
    """Real M15 candles bracketing that session's actual most recent
    window — None if the broker has nothing for that window (e.g. a
    genuinely illiquid instrument with gaps), handled by the caller as
    "no session context available" rather than a crash. If that session is
    still in progress (its close time is in the future — this function is
    also used to read a session's own progress-so-far, not just completed
    ones), the fetch window is clamped to now: OANDA rejects a 'to'
    timestamp in the future, and "everything so far" is the honest answer
    for an unfinished session anyway."""
    open_time, close_time = most_recent_session_window(session_name, now_utc)
    fetch_to = min(close_time, now_utc)
    if fetch_to <= open_time:
        return None  # session opened seconds ago — no real range to report yet
    candles = await broker.get_candles_range(instrument, "M15", open_time, fetch_to)
    if not candles:
        return None
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    open_price = candles[0].open
    close_price = candles[-1].close
    return PriorSessionRange(
        session_name=session_name, open_time=open_time, close_time=close_time,
        high=max(highs), low=min(lows), open_price=open_price, close_price=close_price,
        range_size=max(highs) - min(lows),
        net_direction=1.0 if close_price > open_price else (-1.0 if close_price < open_price else 0.0),
    )


# A breakout beyond the full prior range (fraction >= 0.5, i.e. price has
# moved half the prior range's own size past its edge) saturates the score
# — self-relative to that range's own size, not a fixed pip count, so it
# means the same thing for a quiet JPY cross as a volatile GBP one.
_BREAKOUT_SATURATION_FRACTION = 0.5
_BASE_SESSION_CONFIDENCE = 0.5
# Damped, not boosted, right at a session's own open — spec of this
# feature (user-requested 2026-08-13): a brand-new session is exactly when
# it's most likely to reverse/correct the prior one rather than confirm
# it, so less certainty is the honest read until it's had time to confirm.
_JUST_OPENED_CONFIDENCE_MULTIPLIER = 0.5
_OVERLAP_CONFIDENCE_MULTIPLIER = 1.3
# Price still inside the prior session's range (no confirmed breakout yet)
# gets only a weak continuation lean toward that prior session's own
# direction, not a confident call — capped well below what a real breakout
# would score.
_WITHIN_RANGE_LEAN_CAP = 0.3


async def pair_session_score(broker, pair: str, now_utc: datetime, current_price: float) -> tuple[float, float]:
    """(score, confidence) — where current price sits relative to the most
    recently handed-off session's real range. Market closed or no session
    currently active -> (0.0, 0.0), same "no opinion" convention every
    other component in this system uses when it has nothing to say.
    `current_price` is passed in rather than fetched here since the caller
    (src/run_loop.py's evaluate_pair) already has a fresh quote for this
    pair every cycle — fetching it twice would double the price-API calls
    across a large instrument list for no benefit."""
    state = current_session_state(now_utc)
    if state.market_closed or not state.active_sessions:
        return 0.0, 0.0

    reference_session = (state.just_opened or state.active_sessions)[0]
    prior_name = previous_session(reference_session)

    prior_range = await compute_prior_session_range(broker, pair, prior_name, now_utc)
    if prior_range is None or prior_range.range_size <= 0:
        return 0.0, 0.0

    if current_price > prior_range.high:
        breakout_fraction = (current_price - prior_range.high) / prior_range.range_size
        score = min(1.0, breakout_fraction / _BREAKOUT_SATURATION_FRACTION)
    elif current_price < prior_range.low:
        breakout_fraction = (prior_range.low - current_price) / prior_range.range_size
        score = -min(1.0, breakout_fraction / _BREAKOUT_SATURATION_FRACTION)
    else:
        position_in_range = (current_price - prior_range.low) / prior_range.range_size  # 0..1
        score = (position_in_range - 0.5) * 2 * _WITHIN_RANGE_LEAN_CAP

    confidence = _BASE_SESSION_CONFIDENCE
    if reference_session in state.just_opened:
        confidence *= _JUST_OPENED_CONFIDENCE_MULTIPLIER
    if state.overlap:
        confidence *= _OVERLAP_CONFIDENCE_MULTIPLIER

    return score, min(1.0, confidence)


# Fixed, moderate — not tuned against any outcome data (there isn't any yet
# for this specific bet), just a plain "worth listening to but not
# overriding everything else" starting confidence. Whether it's actually
# right is exactly what the challenger's shadow track record is for.
_NY_REVERSAL_CONFIDENCE = 0.5


def ny_open_reversal_signal(london_range: PriorSessionRange | None) -> tuple[float, float]:
    """Concrete session-transition playbook rule (user-requested
    2026-08-13): "sometimes NY corrects what London did." Unlike
    `pair_session_score` above (which deliberately stays agnostic on
    direction — a breakout is scored the same whether it continues or
    reverses the prior session), this is a genuine, one-sided directional
    bet: it always bets *against* London's own net direction. That's only
    safe to do here because this function is wired in purely as a
    champion/challenger shadow view (src/challengers/definitions.py) — it
    never reaches the risk governor or a broker on its own, and stands or
    falls on real logged challenger-vs-champion performance (Autonomous
    Upgrade Spec sec. 15) before anything built on it could ever be
    promoted. Caller gates this to New York's own just-opened window
    (that's the specific transition the user described) and passes None
    outside it or when London had no net direction, in which case this
    correctly has nothing to say."""
    if london_range is None or london_range.net_direction == 0.0:
        return 0.0, 0.0
    return -london_range.net_direction, _NY_REVERSAL_CONFIDENCE


def should_run_cycle(now_utc: datetime) -> tuple[bool, str]:
    """(should_run, reason) for the *scheduled* decision-cycle entrypoint
    only (user-requested 2026-08-13: "scan more frequently / adapt
    cadence") — never applied to a manual dashboard-triggered run, which
    always runs immediately since a human just explicitly asked for it.

    The scheduled task's own trigger interval is the tightest cadence this
    can ever run at; this function decides, on each tick, whether that
    tick should actually pay for a full cycle (68 instruments x 3 horizons
    of real broker calls). It's stateless — a fresh `python -m
    src.run_loop --once` process every tick has no memory of the last one
    — so throttling during quiet periods is done via clock alignment
    (`now_utc.minute % 4 < 2`) rather than a counter, and is deliberately
    conservative: any real ambiguity (overlap, a session just opening or
    about to close) always runs at full speed rather than risking a missed
    transition."""
    state = current_session_state(now_utc)
    if state.market_closed:
        return False, "market closed (weekend) — nothing to scan"
    if state.overlap:
        return True, "session overlap — highest-liquidity window, full speed"
    if state.just_opened or state.closing_soon:
        return True, "session transition window — full speed"
    return (
        now_utc.minute % 4 < 2,
        "quiet single-session period — throttled cadence",
    )

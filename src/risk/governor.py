"""Risk Governor (blueprint sec. 3 layer 8, sec. 7 gates, sec. 8 controls).

This module is deliberately independent of the prediction/decision code in
src/decision — it never imports it, and it is the only thing allowed to
turn a TradeDecision into an approved, sized order. Per sec. 8: "The
prediction model is never permitted to increase its own risk limit because
it 'feels confident.'" Every gate here can only make a trade smaller or
reject it outright; none of them can increase size or override another
gate's rejection.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.data.db import risk_state as risk_state_table
from src.data.db import risk_state_weekly as risk_state_weekly_table
from src.data.db import upsert_insert as insert
from src.models.track_record import instrument_reliability_multiplier

# --- sec. 8 controls (defaults) ---
RISK_PER_TRADE_PCT = 0.0025  # 0.25% of equity, per sec. 8 default
RISK_PER_TRADE_CEILING_PCT = 0.01  # absolute ceiling, sec. 8
DAILY_LOSS_LIMIT_PCT = 0.015  # 1.5%, sec. 8 "example research setting"
MAX_CONCURRENT_POSITIONS = 3
MAX_CORRELATED_EXPOSURE_PCT = 0.05  # cap aggregate same-USD-direction notional / NAV

# --- sec. 7 gates (defaults) ---
# Per-feed staleness thresholds: a price stream stale for 30s is a real
# problem, but an hourly candle or a macro calendar snapshot that's an hour
# old is completely normal. One global threshold would either be too loose
# for prices or reject macro/candle data constantly.
#
# `price` age is measured against OANDA's own quote timestamp — the age of
# the last actual tick, not "how long ago we polled." OANDA's pricing
# endpoint returns the last known price; during a quiet moment for a given
# pair it's completely normal for 30-45s to pass between ticks even on a
# healthy connection (this system polls per decision cycle, sec. 2.2 — it
# doesn't hold a live stream open). A 30s cutoff mistook that for a dead
# feed. 90s still catches a genuinely broken connection well within a
# typical 5-minute cycle interval, without rejecting on normal tick gaps.
FRESHNESS_THRESHOLDS_SECONDS = {
    "price": 90.0,
    "candles": 7200.0,  # 2x an H1 candle's own period
}
DEFAULT_FRESHNESS_THRESHOLD_SECONDS = 30.0
MAX_SPREAD_MULTIPLE = 3.0  # reject if current spread > N x recent median spread
MIN_CONFIDENCE = 0.35
EVENT_LOCKOUT_MINUTES = 30

# Autonomous Upgrade Spec sec. 11 "Market Regime Router": "High volatility ->
# reduce size". This is the only regime-routing behavior that belongs in the
# risk governor rather than the fusion layer (src/decision/fusion.py's
# REGIME_WEIGHT_MULTIPLIERS / REGIME_THRESHOLD_MULTIPLIERS handle the rest) —
# this module's own docstring is explicit that it's the only thing allowed
# to shrink a position, so sizing-by-regime lives here, not in fusion.
# SHOCK gets the deepest cut since ATR itself is already elevated going into
# the stop-distance calc, compounding the risk-per-trade cut on top of an
# already-wider stop. Regimes not listed (TREND/RANGE/UNKNOWN) size normally
# — UNKNOWN is already handled upstream via fusion's higher action threshold
# (fewer trades reach the governor at all in that regime), not via sizing.
REGIME_SIZE_MULTIPLIERS: dict[str, float] = {
    "HIGH_VOLATILITY": 0.5,
    "SHOCK": 0.25,
}

# Confidence-scaled sizing floor (spec sec. 16) — see the sizing gate for
# the full derivation. Never below 50% of the baseline risk, so a trade
# that only just cleared MIN_CONFIDENCE still gets a meaningful size, not
# a token one.
CONFIDENCE_SIZE_FLOOR = 0.5

# Weekly loss limit (spec sec. 17): daily limits alone can't catch a slow
# bleed spread across several down days that never individually breach
# DAILY_LOSS_LIMIT_PCT. Wider than the daily limit for the obvious reason —
# a week has more trades and more natural variance than a day.
WEEKLY_LOSS_LIMIT_PCT = 0.04

# Price-drift / slippage guard (spec sec. 17): the decision's reference_price
# is however old the signal is by the time it reaches execution (human
# review can take a while). If the live price has since moved against the
# trade by more than this fraction of the planned stop distance, the setup
# the decision was reasoning about may no longer hold — reject and let the
# next cycle re-evaluate fresh, rather than chase a price that's already
# moved a meaningful fraction of the way to the stop.
PRICE_DRIFT_STOP_RATIO = 0.5


def check_calendar_event_risk(
    economic_events: list[dict[str, Any]],
    pair: str,
    now: datetime,
    lockout_minutes: int = EVENT_LOCKOUT_MINUTES,
    source: str = "ForexFactory",
) -> tuple[bool, bool]:
    """Returns (calendar_covers_currency, upcoming_tier1_event_within_lockout)
    for a pair's two currencies, from ForexFactory-sourced forward-looking
    rows (src/macro/forex_factory.py — the only source in this system with
    real event timing + impact tier). FRED-sourced rows never set
    `calendar_covers_currency=True` on their own: FRED gives realized
    values, not a forward schedule, so it can't answer "is something about
    to happen" (see src/macro/fred.py docstring).

    The lockout window is symmetric (before AND after the release) — there
    is no dedicated event-reaction strategy built yet (blueprint sec. 9), so
    the honest thing to do is stay flat through the release and the
    immediate aftermath, not just up to T0.

    Non-forex instruments (Alpaca equities/crypto) have no currency-calendar
    concept at all — this ForexFactory-sourced calendar only ever covers FX
    currencies, so `calendar_covers_currency=True` (deliberately, not False)
    lets the event gate downstream pass rather than reject every equity/
    crypto trade for "no calendar coverage", which would otherwise
    permanently lock them out."""
    if "_" not in pair:
        return True, False
    base, quote = pair.split("_")
    currencies = {base, quote}
    relevant = [
        e for e in economic_events
        if e.get("source") == source and e.get("currency") in currencies
    ]
    calendar_covers_currency = len(relevant) > 0

    window_start = now - timedelta(minutes=lockout_minutes)
    window_end = now + timedelta(minutes=lockout_minutes)
    upcoming_tier1 = any(
        e.get("importance") == "high" and window_start <= e["event_time"] <= window_end
        for e in relevant
    )
    return calendar_covers_currency, upcoming_tier1


def usd_direction_of_trade(pair: str, action: str) -> str:
    """Which side of USD a BUY/SELL on this pair puts you net on.
    XXX_USD: BUY = long the base currency = short USD; SELL = long USD.
    USD_XXX: BUY = long USD; SELL = short USD.
    Neither leg USD (a true cross pair, e.g. AUD_CAD — real once the full
    instrument universe is in play, not just the original 5 USD-pairs):
    the trade creates no *direct* USD-denominated directional exposure the
    way a USD pair does, so it's tagged "no_usd_leg" rather than being
    silently miscounted as one side or the other — real bug this replaced,
    the old code fell through to treating any non-"_USD"-suffixed pair as
    if USD were the base, which is wrong for a pair where USD isn't
    involved at all.

    Alpaca equities/crypto are USD-denominated directly (no base/quote
    split at all), and their risk is genuinely different in kind from FX
    correlation (single-name/market risk, not currency-pair risk) — tagged
    with their own asset-class-specific direction so the correlation gate's
    aggregate exposure buckets don't conflate a long AAPL position with a
    long EUR_USD position."""
    if "/" in pair:  # crypto, e.g. BTC/USD — always USD-quoted
        return "crypto_long" if action == "BUY" else "crypto_short"
    if "_" not in pair:  # equity ticker, e.g. AAPL — USD-denominated
        return "equity_long" if action == "BUY" else "equity_short"
    base, quote = pair.split("_")
    if quote == "USD":
        return "long_usd" if action == "SELL" else "short_usd"
    if base == "USD":
        return "long_usd" if action == "BUY" else "short_usd"
    return "no_usd_leg"


def usd_value_per_unit(pair: str, current_price: float, usd_rates: dict[str, float] | None = None) -> float:
    """USD P&L per 1 unit of base currency moved by 1 unit of quote price.
    Direct for USD-account pairs where USD is base or quote. For a true
    cross pair (neither leg USD — the majority of pairs once the full
    instrument universe is in play, e.g. AUD_CAD), P&L accrues in the quote
    currency and needs converting to USD via a live rate; `usd_rates` (built
    once per cycle by src/run_loop.py's `_build_usd_conversion_rates`, from
    a real batch price fetch of whichever direct-to-USD pair each currency
    actually has) supplies that. Still raises if a genuinely unavailable
    conversion is requested — fails loud rather than silently mispricing
    risk, same posture as before this was generalized beyond USD-leg pairs.

    Alpaca equities (bare ticker, e.g. AAPL) and crypto (BASE/USD, e.g.
    BTC/USD) are both already priced directly in USD — a $1 move in either
    is exactly $1 of USD P&L per 1 unit of position, the same relationship
    as a quote-is-USD FX pair, so this short-circuits to the same 1.0
    before attempting a base/quote split that doesn't apply to them."""
    if "/" in pair or "_" not in pair:
        return 1.0
    base, quote = pair.split("_")
    if quote == "USD":
        return 1.0
    if base == "USD":
        return 1.0 / current_price
    if usd_rates and quote in usd_rates:
        return usd_rates[quote]
    raise ValueError(
        f"usd_value_per_unit: {pair} has no USD leg and no usd_rates entry for {quote} — "
        "cannot price risk for this pair without a live conversion rate"
    )


@dataclass
class GateResult:
    name: str
    passed: bool
    detail: str


@dataclass
class RiskDecision:
    approved: bool
    reason: str
    size_units: int | None
    gates: list[GateResult] = field(default_factory=list)


def _get_or_init_day_state(engine: Engine, user_id: int, balance: float, now: datetime) -> dict[str, Any]:
    day_key = now.strftime("%Y-%m-%d")
    with engine.begin() as conn:
        row = conn.execute(
            select(risk_state_table).where(risk_state_table.c.user_id == user_id, risk_state_table.c.day == day_key)
        ).mappings().first()
        if row:
            return dict(row)
        stmt = insert(risk_state_table).values(
            user_id=user_id,
            day=day_key,
            day_start_balance=balance,
            kill_switch_active=False,
            kill_switch_reason=None,
            updated_at=now,
        )
        conn.execute(stmt)
        return {
            "day": day_key,
            "day_start_balance": balance,
            "kill_switch_active": False,
            "kill_switch_reason": None,
        }


def _get_or_init_week_state(engine: Engine, user_id: int, balance: float, now: datetime) -> dict[str, Any]:
    iso_year, iso_week, _ = now.isocalendar()
    week_key = f"{iso_year}-W{iso_week:02d}"
    with engine.begin() as conn:
        row = conn.execute(
            select(risk_state_weekly_table).where(
                risk_state_weekly_table.c.user_id == user_id, risk_state_weekly_table.c.iso_week == week_key
            )
        ).mappings().first()
        if row:
            return dict(row)
        conn.execute(
            insert(risk_state_weekly_table).values(
                user_id=user_id, iso_week=week_key, week_start_balance=balance, updated_at=now,
            )
        )
        return {"iso_week": week_key, "week_start_balance": balance}


def set_kill_switch(
    engine: Engine, user_id: int, active: bool, reason: str | None, now: datetime | None = None
) -> None:
    now = now or datetime.now(timezone.utc)
    day_key = now.strftime("%Y-%m-%d")
    with engine.begin() as conn:
        stmt = insert(risk_state_table).values(
            user_id=user_id,
            day=day_key,
            day_start_balance=0.0,
            kill_switch_active=active,
            kill_switch_reason=reason,
            updated_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["user_id", "day"],
            set_={
                "kill_switch_active": active,
                "kill_switch_reason": reason,
                "updated_at": now,
            },
        )
        conn.execute(stmt)


def evaluate(
    engine: Engine,
    *,
    user_id: int,
    instrument: str,
    action: str,
    confidence: float,
    stop_distance: float | None,
    current_price: float,
    current_spread: float,
    recent_median_spread: float | None,
    data_freshness_seconds: dict[str, float],
    account_balance: float,
    account_nav: float,
    open_position_count: int,
    open_positions_usd_direction: dict[str, float],  # {"long_usd": notional, "short_usd": notional}
    component_scores: dict[str, float],  # e.g. {"price": 0.4, "macro": -0.1, "news": 0.05}
    calendar_covers_currency: bool,
    upcoming_tier1_event_within_lockout: bool,
    reconciliation_ok: bool,
    regime: str | None = None,
    reference_price: float | None = None,
    usd_rates: dict[str, float] | None = None,
    now: datetime | None = None,
) -> RiskDecision:
    now = now or datetime.now(timezone.utc)
    gates: list[GateResult] = []

    def gate(name: str, passed: bool, detail: str) -> bool:
        gates.append(GateResult(name, passed, detail))
        return passed

    # --- kill-switch gate (checked first; nothing below can override it) ---
    day_state = _get_or_init_day_state(engine, user_id, account_balance, now)
    daily_pl_pct = (account_nav - day_state["day_start_balance"]) / day_state["day_start_balance"] if day_state["day_start_balance"] else 0.0

    if day_state["kill_switch_active"]:
        gate("kill_switch", False, f"already active: {day_state['kill_switch_reason']}")
        return RiskDecision(False, "kill switch active", None, gates)

    if not reconciliation_ok:
        set_kill_switch(engine, user_id, True, "position reconciliation failed", now)
        gate("kill_switch", False, "reconciliation failed — kill switch engaged")
        return RiskDecision(False, "reconciliation failed", None, gates)

    if daily_pl_pct <= -DAILY_LOSS_LIMIT_PCT:
        set_kill_switch(engine, user_id, True, f"daily loss {daily_pl_pct:.2%} breached {-DAILY_LOSS_LIMIT_PCT:.2%}", now)
        gate("kill_switch", False, f"daily loss limit breached: {daily_pl_pct:.2%}")
        return RiskDecision(False, "daily loss limit breached", None, gates)

    # Weekly loss limit (spec sec. 17) — catches a slow bleed spread across
    # several down days that never individually breach the daily limit.
    week_state = _get_or_init_week_state(engine, user_id, account_balance, now)
    weekly_pl_pct = (
        (account_nav - week_state["week_start_balance"]) / week_state["week_start_balance"]
        if week_state["week_start_balance"] else 0.0
    )
    if weekly_pl_pct <= -WEEKLY_LOSS_LIMIT_PCT:
        set_kill_switch(engine, user_id, True, f"weekly loss {weekly_pl_pct:.2%} breached {-WEEKLY_LOSS_LIMIT_PCT:.2%}", now)
        gate("kill_switch", False, f"weekly loss limit breached: {weekly_pl_pct:.2%}")
        return RiskDecision(False, "weekly loss limit breached", None, gates)

    gate("kill_switch", True, f"daily P/L {daily_pl_pct:.2%}, weekly P/L {weekly_pl_pct:.2%}, no reconciliation failure")

    # --- action gate: nothing to size for NO_TRADE ---
    if action == "NO_TRADE":
        gate("action", True, "decision was NO_TRADE — no sizing needed")
        return RiskDecision(False, "decision was NO_TRADE", None, gates)

    # --- freshness gate (per-feed thresholds; see FRESHNESS_THRESHOLDS_SECONDS) ---
    stale = {
        k: v
        for k, v in data_freshness_seconds.items()
        if v > FRESHNESS_THRESHOLDS_SECONDS.get(k, DEFAULT_FRESHNESS_THRESHOLD_SECONDS)
    }
    if not gate("freshness", not stale, f"stale feeds: {stale}" if stale else "all feeds fresh"):
        return RiskDecision(False, "stale data", None, gates)

    # --- spread gate ---
    if recent_median_spread and recent_median_spread > 0:
        spread_ok = current_spread <= MAX_SPREAD_MULTIPLE * recent_median_spread
        detail = f"current={current_spread:.5f} vs {MAX_SPREAD_MULTIPLE}x median={recent_median_spread:.5f}"
    else:
        spread_ok = True
        detail = "no recent spread history yet — gate not evaluated"
    if not gate("spread", spread_ok, detail):
        return RiskDecision(False, "spread abnormally wide", None, gates)

    # --- price-drift guard (spec sec. 17 "slippage guard") ---
    # reference_price is the price at signal time; current_price is fresh as
    # of right now. A long human-review gap between the two can leave the
    # decision's reasoning (entry, stop, target — all anchored to
    # reference_price) stale even though every other gate above still
    # passes. None means the caller didn't supply one (e.g. older code
    # paths) — not evaluated rather than assumed fine.
    if reference_price is not None and stop_distance:
        drift = abs(current_price - reference_price)
        drift_ok = drift <= PRICE_DRIFT_STOP_RATIO * stop_distance
        detail = f"price moved {drift:.5f} since signal vs {PRICE_DRIFT_STOP_RATIO:.0%} of {stop_distance:.5f} stop distance"
    else:
        drift_ok = True
        detail = "no reference price or stop distance supplied — gate not evaluated"
    if not gate("price_drift", drift_ok, detail):
        return RiskDecision(False, "price drifted too far from signal price", None, gates)

    # --- event gate ---
    if not calendar_covers_currency:
        if not gate("event", False, f"no calendar coverage for {instrument} — cannot verify tier-1 event risk"):
            return RiskDecision(False, "unverified event risk (no calendar source connected)", None, gates)
    elif upcoming_tier1_event_within_lockout:
        if not gate("event", False, f"tier-1 release within {EVENT_LOCKOUT_MINUTES}m"):
            return RiskDecision(False, "tier-1 event lockout", None, gates)
    else:
        gate("event", True, "no tier-1 event within lockout window")

    # --- confidence gate ---
    if not gate("confidence", confidence >= MIN_CONFIDENCE, f"confidence={confidence:.2f} vs min={MIN_CONFIDENCE}"):
        return RiskDecision(False, "confidence below threshold", None, gates)

    # --- agreement gate ---
    direction = 1 if action == "BUY" else -1
    disagreeing = {k: v for k, v in component_scores.items() if v * direction < -0.3}
    if not gate("agreement", not disagreeing, f"strong contradiction: {disagreeing}" if disagreeing else "no strong contradiction"):
        return RiskDecision(False, "strong component disagreement", None, gates)

    # --- portfolio/correlation gate ---
    if not gate("max_positions", open_position_count < MAX_CONCURRENT_POSITIONS,
                f"{open_position_count} open vs max {MAX_CONCURRENT_POSITIONS}"):
        return RiskDecision(False, "max concurrent positions reached", None, gates)

    usd_direction_key = usd_direction_of_trade(instrument, action)
    if usd_direction_key == "no_usd_leg":
        # A true cross pair (e.g. AUD_CAD) creates no *direct* USD-
        # denominated directional exposure the way a USD pair does — this
        # gate is specifically about aggregate same-USD-direction notional,
        # so it doesn't apply here rather than being force-fit into either
        # bucket (the old bug this replaced miscounted it as one).
        gate("correlation", True, "no direct USD leg — correlation cap doesn't apply to this pair")
    else:
        projected_exposure = open_positions_usd_direction.get(usd_direction_key, 0.0)
        correlation_ok = (projected_exposure / account_nav if account_nav else 0.0) < MAX_CORRELATED_EXPOSURE_PCT
        gate("correlation", correlation_ok,
             f"{usd_direction_key} exposure {projected_exposure:.0f} / NAV {account_nav:.0f}")
        if not correlation_ok:
            return RiskDecision(False, "correlated USD exposure cap reached", None, gates)

    # --- risk sizing gate ---
    if not stop_distance or stop_distance <= 0:
        gate("sizing", False, "no valid stop distance — cannot size a bounded-risk position")
        return RiskDecision(False, "missing stop distance", None, gates)

    risk_pct = min(RISK_PER_TRADE_PCT, RISK_PER_TRADE_CEILING_PCT)
    regime_multiplier = REGIME_SIZE_MULTIPLIERS.get(regime or "", 1.0)
    # Spec sec. 16: "use confidence to control... how much it risks." This
    # module's own docstring is explicit that nothing here may ever
    # INCREASE size — confidence_multiplier is deliberately bounded to
    # [CONFIDENCE_SIZE_FLOOR, 1.0], scaled from the confidence gate's own
    # MIN_CONFIDENCE floor (a trade right at the confidence gate's minimum
    # gets the smallest size that still clears it; a maximally confident
    # one gets the full baseline risk_pct, never more).
    confidence_span = max(1e-9, 1.0 - MIN_CONFIDENCE)
    confidence_multiplier = CONFIDENCE_SIZE_FLOOR + (1.0 - CONFIDENCE_SIZE_FLOOR) * min(
        1.0, max(0.0, (confidence - MIN_CONFIDENCE) / confidence_span)
    )
    # User-requested 2026-08-14 ("maximize profit on instruments which are
    # profitable"): a real, self-relative per-instrument track record,
    # same never-boost-only-dampen invariant as regime/confidence above —
    # see src/models/track_record.py's docstring for the full derivation
    # and the real one-directional-trend trap (USD_TRY, 100% hit rate but
    # 306/306 BUY-only) its two-sided-evidence guard exists to catch.
    track_record_multiplier, track_record_detail = instrument_reliability_multiplier(engine, user_id, instrument)
    size_multiplier = regime_multiplier * confidence_multiplier * track_record_multiplier
    risk_amount_usd = account_balance * risk_pct * size_multiplier
    try:
        per_unit_usd_risk = stop_distance * usd_value_per_unit(instrument, current_price, usd_rates)
    except ValueError as exc:
        gate("sizing", False, str(exc))
        return RiskDecision(False, str(exc), None, gates)

    size_units = int(risk_amount_usd / per_unit_usd_risk) if per_unit_usd_risk > 0 else 0
    size_detail = (
        f"{size_units} units at {risk_pct:.2%} risk (${risk_amount_usd:.2f}) "
        f"[regime x{regime_multiplier:.2f}, confidence x{confidence_multiplier:.2f}, "
        f"track_record x{track_record_multiplier:.2f} ({track_record_detail})]"
    )
    if not gate("sizing", size_units > 0, size_detail):
        return RiskDecision(False, "computed size is zero", None, gates)

    return RiskDecision(True, "approved", size_units, gates)

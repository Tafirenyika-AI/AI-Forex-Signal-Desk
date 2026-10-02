"""Equity V2 Phase 13 — equity risk governor extensions.

Extends src/risk/governor.py's gate vocabulary for equity-specific risks
it does not yet cover: real SECTOR concentration (the existing
correlation gate's usd_direction_of_trade() only buckets equities into
one crude "equity_long"/"equity_short" pair, conflating a long AAPL
position with a long XOM position as if they carried the same risk),
an earnings-event lockout (the same "don't trade right before a known
high-impact event" pattern src/risk/governor.py's own
check_calendar_event_risk already established for forex macro releases,
applied to Phase 6's real equity_news/company_events EARNINGS tags), a
rolling peak-to-trough drawdown circuit breaker (more general than the
existing daily/weekly loss limits, which reset at calendar boundaries
rather than tracking a trailing high-water mark), and a minimum-expected-
edge-after-costs floor (so a technically-positive-confidence signal
whose expected edge doesn't even clear round-trip trading costs isn't
treated as worth taking).

CRITICAL, deliberate scope boundary: this module is PURE, ADDITIVE,
STANDALONE logic — nothing here is wired into src/risk/governor.py's own
evaluate()/revalidate_before_submission() gate chain, and nothing here is
imported by src/run_loop.py or src/execution/service.py. The existing
forex governor (sec. 8's live safety rails, battle-tested across this
project's entire history) is completely untouched. Wiring these new
gates into the live approval chain is a deliberate, separate, carefully-
reviewed next step — not done in this pass — matching how Phase 10's
challenger models were built shadow-only before any promotion, and
doubly appropriate here since a risk-gate bug is categorically worse than
a shadow model simply not being used yet (it could let through a trade
that should have been blocked). "The risk governor always has final veto"
(the brief's own Phase 13 instruction) means any eventual integration
must ONLY be able to make the real governor MORE conservative, layering
in as an additional required-pass gate, never replacing or loosening an
existing one.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.data.db import equity_entities as equity_entities_table
from src.data.db import equity_news as equity_news_table
from src.risk.governor import GateResult

# Same sane default this project's own Phase 12 portfolio backtester
# established after finding, live, that risk-based position sizing alone
# can allocate unrealistic fractions of equity to one correlated sector.
DEFAULT_MAX_SECTOR_EXPOSURE_PCT = 0.40

# Same pattern as src/risk/governor.py's own EVENT_LOCKOUT_MINUTES for
# forex macro releases, applied to earnings specifically: a real move at
# an earnings print can gap through any pre-set stop, so a lockout window
# on BOTH sides of the event (not just before it, since announcement
# timing within a trading day can lag the lockout's own detection) is the
# conservative default.
DEFAULT_EARNINGS_LOCKOUT_HOURS_BEFORE = 24.0
DEFAULT_EARNINGS_LOCKOUT_HOURS_AFTER = 4.0

# A trailing high-water-mark drawdown breaker, distinct from governor.py's
# own DAILY_LOSS_LIMIT_PCT/WEEKLY_LOSS_LIMIT_PCT (which reset at calendar
# boundaries) -- this tracks the worst peak-to-trough decline regardless
# of what day/week it spans, matching how a real drawdown is usually
# discussed and limited in practice.
DEFAULT_MAX_TRAILING_DRAWDOWN_PCT = 0.10

# A minimum estimated round-trip cost assumption for equities -- Alpaca is
# commission-free, but real slippage/spread still exists; a small, clearly
# conservative placeholder until this project has a real measured
# equity spread series (candles don't carry one yet, same disclosed gap
# src/backtest/engine.py's own ASSUMED_SPREAD_PIPS section already notes
# for forex).
DEFAULT_ASSUMED_ROUND_TRIP_COST_PCT = 0.0005  # 5 basis points


def equity_sector_concentration_gate(
    proposed_sector: str | None,
    proposed_dollars: float,
    open_positions_by_sector: dict[str, float],  # sector -> current dollars allocated
    account_equity: float,
    max_sector_exposure_pct: float = DEFAULT_MAX_SECTOR_EXPOSURE_PCT,
) -> GateResult:
    """A real sector-aware concentration cap, distinct from governor.py's
    own crude "all longs vs all shorts" correlation bucket — a new long
    NVDA on top of existing long AAPL/MSFT positions correctly reads as
    concentrated Technology risk here, where the existing gate would only
    ever see three equally-weighted "equity_long" entries."""
    if proposed_sector is None or account_equity <= 0:
        return GateResult("equity_sector_concentration", True, "no known sector for this ticker — gate doesn't apply")
    current_sector_dollars = open_positions_by_sector.get(proposed_sector, 0.0)
    projected_pct = (current_sector_dollars + proposed_dollars) / account_equity
    passed = projected_pct <= max_sector_exposure_pct
    detail = (f"{proposed_sector}: ${current_sector_dollars:,.0f} existing + ${proposed_dollars:,.0f} proposed "
              f"= {projected_pct:.1%} of ${account_equity:,.0f} equity (cap {max_sector_exposure_pct:.0%})")
    return GateResult("equity_sector_concentration", passed, detail)


def single_name_concentration_gate(
    proposed_dollars: float, existing_dollars_in_same_ticker: float, account_equity: float,
    max_single_name_pct: float = DEFAULT_MAX_SECTOR_EXPOSURE_PCT / 2,  # a single name should never alone eat a whole sector's budget
) -> GateResult:
    if account_equity <= 0:
        return GateResult("equity_single_name_concentration", True, "no account equity to evaluate against")
    projected_pct = (existing_dollars_in_same_ticker + proposed_dollars) / account_equity
    passed = projected_pct <= max_single_name_pct
    detail = f"{projected_pct:.1%} of equity in this single name (cap {max_single_name_pct:.0%})"
    return GateResult("equity_single_name_concentration", passed, detail)


def trailing_drawdown_circuit_breaker(
    equity_curve_high_water_mark: float, current_equity: float,
    max_trailing_drawdown_pct: float = DEFAULT_MAX_TRAILING_DRAWDOWN_PCT,
) -> GateResult:
    """Distinct from governor.py's own daily/weekly loss limits (which
    reset at calendar boundaries) -- this halts new entries once the
    account has drawn down more than max_trailing_drawdown_pct from its
    own all-time (or caller-defined-window) high, regardless of which
    day/week that decline happened to span."""
    if equity_curve_high_water_mark <= 0:
        return GateResult("equity_trailing_drawdown", True, "no high-water mark established yet")
    drawdown_pct = (equity_curve_high_water_mark - current_equity) / equity_curve_high_water_mark
    passed = drawdown_pct < max_trailing_drawdown_pct
    detail = f"{drawdown_pct:.1%} below high-water mark ${equity_curve_high_water_mark:,.0f} (breaker at {max_trailing_drawdown_pct:.0%})"
    return GateResult("equity_trailing_drawdown", passed, detail)


def minimum_expected_edge_after_costs_gate(
    expected_move_pct: float, confidence: float,
    assumed_round_trip_cost_pct: float = DEFAULT_ASSUMED_ROUND_TRIP_COST_PCT,
) -> GateResult:
    """A technically-positive-confidence signal whose confidence-weighted
    expected move doesn't even clear estimated round-trip costs has no
    real edge to capture — reject before NUMBER it, not after a long
    string of break-even-or-worse trades reveals it empirically."""
    expected_edge_pct = abs(expected_move_pct) * confidence
    passed = expected_edge_pct > assumed_round_trip_cost_pct
    detail = (f"expected edge {expected_edge_pct:.4%} ({expected_move_pct:.4%} move x {confidence:.0%} confidence) "
              f"vs assumed round-trip cost {assumed_round_trip_cost_pct:.4%}")
    return GateResult("equity_minimum_edge_after_costs", passed, detail)


def fetch_equity_entity_sectors(engine: Engine, tickers: list[str]) -> dict[str, str | None]:
    """ticker -> sector (Phase 7's equity_entities.sector), None for a
    ticker Phase 7 hasn't enriched yet — never guessed."""
    if not tickers:
        return {}
    with engine.connect() as conn:
        rows = conn.execute(
            select(equity_entities_table.c.ticker, equity_entities_table.c.sector)
            .where(equity_entities_table.c.ticker.in_([t.upper() for t in tickers]))
        ).fetchall()
    found = {r.ticker: r.sector for r in rows}
    return {t.upper(): found.get(t.upper()) for t in tickers}


def upcoming_earnings_lockout_gate(
    engine: Engine, ticker: str, now: datetime,
    lockout_hours_before: float = DEFAULT_EARNINGS_LOCKOUT_HOURS_BEFORE,
    lockout_hours_after: float = DEFAULT_EARNINGS_LOCKOUT_HOURS_AFTER,
) -> GateResult:
    """Looks for a real EARNINGS-classified equity_news article (Phase 6)
    for this ticker whose publish_time falls within the lockout window
    around `now`. Uses the article's own publish_time as a proxy for "an
    earnings event is imminent or just happened" -- a real, disclosed
    approximation (Phase 6's classifier tags news ABOUT an event, not a
    structured forward earnings CALENDAR date, which this project doesn't
    have a reliable source for yet; company_events exists in the schema
    for exactly this but has no real ingester feeding it yet, a known gap
    carried from Phase 3's own design). No article in the window is a
    pass, not an assumption that nothing is happening -- disclosed, not
    hidden, as a real coverage limitation of this gate."""
    window_start = now - timedelta(hours=lockout_hours_after)
    window_end = now + timedelta(hours=lockout_hours_before)
    padded_tickers = "," + equity_news_table.c.tickers + ","
    with engine.connect() as conn:
        row = conn.execute(
            select(equity_news_table.c.publish_time)
            .where(
                padded_tickers.like(f"%,{ticker.upper()},%"),
                equity_news_table.c.event_type == "EARNINGS",
                equity_news_table.c.publish_time >= window_start,
                equity_news_table.c.publish_time <= window_end,
            )
            .limit(1)
        ).first()
    if row is None:
        return GateResult("equity_earnings_lockout", True, "no EARNINGS-tagged article in the lockout window (see this gate's own coverage-limitation note)")
    return GateResult("equity_earnings_lockout", False, f"EARNINGS-tagged article published {row.publish_time.isoformat()} — inside the lockout window")

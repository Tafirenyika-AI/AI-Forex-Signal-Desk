"""AI Trading Desk V4 — LEGACY_OPEN_POSITION classification (brief Section 2).

"Introduce a LEGACY_OPEN_POSITION classification for positions existing
before V4 activation. V4 may observe legacy positions and incorporate
their exposure into portfolio risk calculations, but it must not alter
them."

This module is the single source of truth any future V4
strategy/scanner/meta-model MUST check before ever proposing a trade in
an instrument — not a suggestion, a hard requirement this module exists
to make easy to honor and hard to accidentally skip.

Safest-possible default, deliberate: if V4 has never been explicitly
activated for a given (user, broker) at all, EVERY instrument is treated
as legacy. There is no way for a new-V4-originated trade to be proposed
in ANY instrument until a human explicitly activates V4 for that
account — which itself takes an honest snapshot of what's currently
open, not an assumption that nothing is.

Snapshot-based, not timestamp-based: legacy status is keyed by SYMBOL at
the moment of activation, not by comparing an individual fill's
timestamp against a cutoff. A single real position on this system is
often built from several separate fills over days with no one clean
"opened_at" to compare (confirmed live during the V4 Phase 0 safety
audit: this account's real AAPL short was 2 tranches, MSFT was 3).  Once
a symbol is in the snapshot, it stays legacy-protected indefinitely —
even past a full close and a later, genuinely new position in the same
symbol. This is a deliberately conservative, disclosed simplification:
the cost of a symbol staying slightly over-protected forever is far
lower than the cost of a wrong "this is unambiguously a new position,
V4 may manage it" call. A human can always perform a genuine
re-activation (a separate, explicit administrative action — not
implemented by this module, since it must never be a silent side effect
of calling activate_v4() twice) if this is ever judged too conservative.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import insert, select
from sqlalchemy.engine import Engine

from src.data.db import v4_activation as v4_activation_table


def _ensure_utc(value: datetime | None) -> datetime | None:
    """Defensive normalization, not a logic fix: SQLite (used by this
    project's own test suite via an in-memory engine) silently drops
    tzinfo on a DateTime(timezone=True) column round-trip, unlike
    production Postgres which preserves it — the same gap already found
    and fixed this project's own way several times (Equity V2 Phases 9
    and 10). A naive value read back out is assumed UTC (every datetime
    this project stores is UTC already), never a silent wrong-timezone
    guess."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


@dataclass(frozen=True)
class V4ActivationState:
    activated: bool
    activated_at: datetime | None
    legacy_symbols: frozenset[str]


def get_activation_state(engine: Engine, user_id: int, broker: str) -> V4ActivationState:
    with engine.connect() as conn:
        row = conn.execute(
            select(v4_activation_table).where(
                v4_activation_table.c.user_id == user_id, v4_activation_table.c.broker == broker,
            )
        ).mappings().first()
    if row is None:
        return V4ActivationState(activated=False, activated_at=None, legacy_symbols=frozenset())
    symbols = frozenset(json.loads(row["legacy_symbols_json"]))
    return V4ActivationState(activated=True, activated_at=_ensure_utc(row["activated_at"]), legacy_symbols=symbols)


def is_legacy_position(engine: Engine, user_id: int, broker: str, instrument: str) -> bool:
    """True if `instrument` must never be touched (closed, reduced,
    increased, reversed, or have its protective orders changed) by any
    NEW V4 component. True — the safe default — if V4 hasn't been
    activated at all yet for this (user, broker)."""
    state = get_activation_state(engine, user_id, broker)
    if not state.activated:
        return True
    return instrument.upper() in state.legacy_symbols


def activate_v4(
    engine: Engine, user_id: int, broker: str, current_open_symbols: list[str], set_by: str,
    now: datetime | None = None,
) -> V4ActivationState:
    """The one-time snapshot: every symbol with a real open position AT
    THIS MOMENT becomes permanently legacy-protected for this (user,
    broker). `current_open_symbols` must come from a REAL, live
    broker.positions() call made immediately before this is called —
    never a cached or assumed list (same "if broker connectivity is
    unavailable, report the limitation and do not assume the account has
    no open positions" instruction the brief's own Section 2 states).

    Deliberately refuses to run twice: calling this a second time for a
    (user, broker) that's already activated would silently
    change/shrink/grow an already-committed protection boundary, which
    is exactly the kind of accidental weakening this module exists to
    prevent. A genuine re-activation is a separate, explicit
    administrative action (delete the row, with a human's deliberate
    sign-off, then call this again) — not something this function does
    as a side effect."""
    now = now or datetime.now(timezone.utc)
    existing = get_activation_state(engine, user_id, broker)
    if existing.activated:
        raise RuntimeError(
            f"V4 is already activated for user_id={user_id} broker={broker} as of "
            f"{existing.activated_at.isoformat()} with {len(existing.legacy_symbols)} legacy symbol(s) — "
            "refusing to silently overwrite an existing activation boundary."
        )
    symbols = sorted({s.upper() for s in current_open_symbols})
    with engine.begin() as conn:
        conn.execute(insert(v4_activation_table).values(
            user_id=user_id, broker=broker, activated_at=now,
            legacy_symbols_json=json.dumps(symbols), set_by=set_by,
        ))
    return V4ActivationState(activated=True, activated_at=now, legacy_symbols=frozenset(symbols))

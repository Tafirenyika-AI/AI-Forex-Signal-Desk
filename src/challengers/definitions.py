"""Challenger definitions (Autonomous Upgrade Spec sec. 15): "Challengers =
new models, new features, new data sources or different weights. All
challengers receive the same market snapshots but cannot execute."

Deliberately one real challenger today, not a speculative framework for
challengers that don't exist yet — CHALLENGERS is a plain list specifically
so a second one is just another entry, not a redesign. The first (and so
far only) challenger tests the one concrete signal this project has
explicitly deferred pending exactly this kind of validation: the
economic-surprise component built in P4 (src/models/surprise_component.py),
never added to the champion's live weights because whether it actually
helps was always this priority's job to prove, not assume.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.data.db import strategy_graveyard as strategy_graveyard_table
from src.decision.fusion import COMPONENT_WEIGHTS, ComponentView
from src.models.surprise_component import pair_surprise_score
from src.models.trading_sessions import ny_open_reversal_signal


@dataclass(frozen=True)
class Challenger:
    name: str
    weights: dict[str, float]
    # Given cycle context, returns an extra ComponentView to add on top of
    # the champion's own (price/macro/cross_market/news), or None when this
    # challenger has nothing new to say this cycle (e.g. no fresh surprise
    # data) — in which case it's simply not logged that cycle, rather than
    # logging a decision indistinguishable from the champion's.
    extra_view: Callable[[dict[str, Any]], ComponentView | None] = field(repr=False)


def _economic_surprise_view(context: dict[str, Any]) -> ComponentView | None:
    score, confidence = pair_surprise_score(
        context["economic_surprises"], context["pair"], context["now"]
    )
    if confidence == 0.0:
        return None
    return ComponentView("surprise", score, confidence, {})


def _ny_open_reversal_view(context: dict[str, Any]) -> ComponentView | None:
    score, confidence = ny_open_reversal_signal(context.get("london_range"))
    if confidence == 0.0:
        return None
    return ComponentView("ny_reversal", score, confidence, {})


CHALLENGERS: list[Challenger] = [
    Challenger(
        name="economic_surprise_v1",
        # fuse()'s combined_score is a confidence-weighted average
        # (Σw·s·c / Σw·c) — invariant to uniformly scaling every weight, so
        # adding "surprise" at 0.15 on top of the champion's existing
        # weights blends it in proportionally without needing to manually
        # shrink the other four first.
        weights={**COMPONENT_WEIGHTS, "surprise": 0.15},
        extra_view=_economic_surprise_view,
    ),
    Challenger(
        name="ny_open_reversal_v1",
        # User-requested 2026-08-13 (see src/models/trading_sessions.py's
        # ny_open_reversal_signal docstring): a directional bet that New
        # York corrects London's net direction, only ever active in NY's
        # own opening transition window. Given real weight only here, as a
        # shadow challenger — never promoted without its own logged
        # win-rate proving the pattern out.
        weights={**COMPONENT_WEIGHTS, "ny_reversal": 0.20},
        extra_view=_ny_open_reversal_view,
    ),
]


def active_challengers(engine: Engine) -> list[Challenger]:
    """Spec sec. 14: buried strategies must not be "repeatedly rediscovered."
    Filters CHALLENGERS against strategy_graveyard on every call rather than
    caching, so a fresh burial takes effect on the very next cycle."""
    with engine.connect() as conn:
        buried = {row[0] for row in conn.execute(select(strategy_graveyard_table.c.strategy_name))}
    return [c for c in CHALLENGERS if c.name not in buried]

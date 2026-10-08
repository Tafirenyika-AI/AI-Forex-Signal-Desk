"""AI Trading Desk V4 — feature flags (brief Section 2).

"All new V4 components must start in read-only or shadow mode without
broker-write permissions" — these env-var flags are how any new V4 code
checks that, and every one of them defaults to the single safest value
the brief itself names. This is a NEW convention, deliberately separate
from this project's existing CLI-argument flags (e.g. src/run_loop.py's
--auto-execute, --enable-trailing-stops): those gate the EXISTING,
already-live scheduled-cycle behavior, read once at process start; these
gate NEW V4 components specifically, and must be checkable at any time
by any new strategy/scanner/meta-model code before it ever proposes a
trade — not bundled into the old mechanism, which these flags must never
be able to accidentally weaken (see each flag's own docstring below).

Read fresh on every call (never cached) — a human flipping one of these
in the real environment must take effect without a process restart,
since restarting could itself be mistaken for the kind of "deploy a new
broker-write capability" action the brief says needs explicit approval
first, not a routine config change.
"""
from __future__ import annotations

import os


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def v4_enabled() -> bool:
    """Master switch for every new V4 component. False (the default)
    means none of them should even run in shadow mode — a genuinely
    inert off position, not just "off but still computing in the
    background." Any new V4 module's own entrypoint should check this
    FIRST and no-op entirely when it's False."""
    return _flag("V4_ENABLED", False)


def v4_shadow_only() -> bool:
    """True (the default, whenever V4 is enabled at all) means every new
    V4 component may compute/log/report but must never reach a broker
    write call. This flag being True must NEVER be bypassable by any
    other flag below — v4_allow_new_paper_orders is the only way out of
    shadow mode, and only for genuinely NEW orders (see its own
    docstring), never for anything touching an existing/legacy
    position."""
    return _flag("V4_SHADOW_ONLY", True)


def v4_auto_promotion() -> bool:
    """False (the default) means a V4 challenger/strategy can never
    self-promote — matches src/evaluation/equity_challenger_promotion.py
    and equity_governor_extensions.py's own already-established posture
    from the Equity V2 effort (a promotion REPORT, never a promote()
    function). Kept as its own explicit flag here so a future V4
    promotion mechanism has a single, obvious place to check, rather than
    relying on "no promote() function exists" as the only guarantee."""
    return _flag("V4_AUTO_PROMOTION", False)


def v4_allow_new_paper_orders() -> bool:
    """False (the default) means even a shadow-cleared, human-reviewed V4
    strategy still cannot place a real (paper-account) order. This is the
    brief's own Phase 7 gate ("Controlled paper execution... only after
    validation and explicit approval"). Flipping this True must NEVER by
    itself be sufficient to let a V4 component touch a position
    classified LEGACY_OPEN_POSITION (src/v4/legacy_position.py) — that
    protection is independent of this flag and must be checked
    separately by any order-placing V4 code path."""
    return _flag("V4_ALLOW_NEW_PAPER_ORDERS", False)


def v4_tradingview_enabled() -> bool:
    """False (the default) — the brief's own Section 17 instruction:
    TradingView integration must remain disabled by default. Even when
    True, a TradingView alert is supplementary evidence for the Decision
    Committee, never a direct trigger — this flag only controls whether
    the webhook RECEIVER accepts and queues alerts at all, never whether
    an alert alone can cause an order."""
    return _flag("V4_TRADINGVIEW_ENABLED", False)

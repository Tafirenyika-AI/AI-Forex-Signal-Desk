"""V4 Priority 8 (brief Section 17 — "Optional TradingView Integration").

Pure validation/normalization logic for incoming TradingView alert
payloads — no HTTP, no DB (see src/integrations/tradingview_server.py for
the real FastAPI receiver that calls this). Kept separate so the actual
decision logic is directly, deterministically testable without spinning up
a server.

"TradingView alerts are supplementary evidence, not automatic commands to
trade" (the brief's own words) — this module's only job is to decide
whether an incoming payload is a real, fresh, authenticated, well-formed
alert worth RECORDING. Nothing here ever creates a trade_intent, calls a
broker, or feeds a live decision path — that integration (the brief's own
"Decision Committee" consuming these as one input among many) is
explicitly future work, not done in this pass.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# How far an alert's own embedded timestamp may drift from when this
# server actually received it before being treated as stale or replayed.
# Generous enough for real network/processing delay, tight enough to
# reject a genuinely old or maliciously-resent payload.
REPLAY_TOLERANCE = timedelta(minutes=5)

VALID_ACTIONS = {"buy", "sell", "close"}

# TradingView's own symbol format is typically "EXCHANGE:TICKER" (e.g.
# "NASDAQ:AAPL", "BINANCE:BTCUSDT") — this project's own instrument naming
# is just the ticker for equities ("AAPL") and "BASE/QUOTE" for crypto
# ("BTC/USD"). Real bug found via this module's own test before shipping:
# an earlier version only stripped a fixed allow-list of EQUITY exchange
# prefixes (NASDAQ/NYSE/...), missing crypto exchanges like BINANCE
# entirely — "BINANCE:BTCUSDT" passed through unstripped. Fixed with a
# generic "any all-caps word followed by a colon" pattern instead of
# maintaining an exchange allow-list, since TradingView's own format is
# consistently EXCHANGE:TICKER regardless of asset class.
_EXCHANGE_PREFIX_RE = re.compile(r"^[A-Z]+:")
# Common crypto quote-currency suffixes TradingView/Binance-style symbols
# use (e.g. "BTCUSDT") — normalized to this project's own "/USD" convention.
# A disclosed, imperfect heuristic: only applied when the resulting base
# ticker is a plausible crypto-ticker length (2-5 chars), to avoid
# misfiring on an equity ticker that happens to end in "USD".
_CRYPTO_QUOTE_SUFFIXES = {"USDT": "USD", "USD": "USD"}


@dataclass(frozen=True)
class AlertValidationResult:
    accepted: bool
    rejection_reason: str | None
    symbol_normalized: str | None
    action: str | None
    alert_time: datetime | None
    strategy_name: str | None
    dedup_key: str | None


def normalize_symbol(raw_symbol: str) -> str:
    symbol = raw_symbol.strip().upper()
    symbol = _EXCHANGE_PREFIX_RE.sub("", symbol, count=1)
    if "/" not in symbol:
        for quote, normalized_quote in _CRYPTO_QUOTE_SUFFIXES.items():
            if symbol.endswith(quote) and len(symbol) > len(quote):
                base = symbol[: -len(quote)]
                if 2 <= len(base) <= 5:
                    return f"{base}/{normalized_quote}"
    return symbol


def _parse_alert_time(raw) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(raw, tz=timezone.utc)
        except (ValueError, OSError):
            return None
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def validate_and_normalize_alert(
    payload: dict, received_at: datetime, expected_secret: str | None,
) -> AlertValidationResult:
    """Pure function: given an already-parsed JSON payload (dict), decides
    accept/reject and, if accepted, the normalized fields to persist.
    `expected_secret`: the real, operator-configured shared secret (from
    an env var, never hardcoded/committed) — None means the server has no
    secret configured at all, which must ALWAYS reject (a webhook that
    accepts anything with no real authentication is not source-verified,
    per the brief's own explicit requirement)."""
    def _reject(reason: str) -> AlertValidationResult:
        return AlertValidationResult(False, reason, None, None, None, None, None)

    if not expected_secret:
        return _reject("no shared secret configured on this server — refusing to accept any alert unauthenticated")

    provided_secret = payload.get("secret")
    if not isinstance(provided_secret, str) or not hmac.compare_digest(provided_secret, expected_secret):
        return _reject("invalid or missing shared secret")

    raw_symbol = payload.get("symbol")
    raw_action = payload.get("action")
    raw_time = payload.get("time")
    if not raw_symbol or not isinstance(raw_symbol, str):
        return _reject("missing or invalid 'symbol' field")
    if not raw_action or not isinstance(raw_action, str):
        return _reject("missing or invalid 'action' field")

    action = raw_action.strip().lower()
    if action not in VALID_ACTIONS:
        return _reject(f"action must be one of {sorted(VALID_ACTIONS)}, got {raw_action!r}")

    alert_time = _parse_alert_time(raw_time)
    if alert_time is None:
        return _reject("missing or unparseable 'time' field (expected ISO8601 or unix epoch seconds)")

    drift = abs((received_at - alert_time).total_seconds())
    if drift > REPLAY_TOLERANCE.total_seconds():
        return _reject(
            f"alert timestamp is {drift:.0f}s away from received time — outside the "
            f"{REPLAY_TOLERANCE.total_seconds():.0f}s replay-protection tolerance"
        )

    symbol_normalized = normalize_symbol(raw_symbol)
    strategy_name = payload.get("strategy")
    strategy_name = strategy_name.strip() if isinstance(strategy_name, str) and strategy_name.strip() else None

    dedup_source = f"{symbol_normalized}|{action}|{alert_time.isoformat()}|{strategy_name or ''}"
    dedup_key = hashlib.sha256(dedup_source.encode("utf-8")).hexdigest()

    return AlertValidationResult(
        accepted=True, rejection_reason=None, symbol_normalized=symbol_normalized,
        action=action, alert_time=alert_time, strategy_name=strategy_name, dedup_key=dedup_key,
    )

"""Equity V2 Phase 2 — real Alpaca WebSocket market-data client.

Preserves src/broker/alpaca.py's AlpacaBroker.stream_prices() (a genuine,
working 5-second-poll implementation, NOT a stub — see that method's own
docstring) exactly as the brief's own instruction asks: "Preserve it
initially as a fallback." Nothing here replaces or touches that method.
This module is additive, standalone infrastructure — a real push-stream
client with reconnect, a heartbeat/watchdog, stale-data detection, and
duplicate-event protection, producing normalized MarketEvent records.

NOT wired into src/run_loop.py's live decision cycle in this pass. The
brief's own architecture is WebSocket -> normalized event -> event store
-> feature engine -> model -> decision engine -> risk governor ->
existing execution service — building the feature-engine/model/decision
consumer side is later-phase work (Phase 9 onward); this phase is the
first box only, proven to genuinely work before anything is built on top
of it. "DO NOT place orders from the WebSocket handler" / "Never:
WebSocket -> order" is true here by construction, not just by promise —
this file imports nothing from src/execution or src/broker.alpaca's
order-placing surface, and connects only to Alpaca's separate read-only
market-data host (stream.data.alpaca.markets), never the trading host
(paper-api.alpaca.markets) AlpacaBroker.place_order() talks to.

Verified live against the real feed before writing the parsing logic
below (not just inferred from docs): connected, authenticated, and
subscribed against this account's real credentials on both
wss://stream.data.alpaca.markets/v2/iex (equities) and
wss://stream.data.alpaca.markets/v1beta3/crypto/us (crypto) — same
auth/subscribe message shape for both — and received real live quote
ticks for BTC/USD to confirm the message format below matches reality.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

import websockets

from src.config import Settings

logger = logging.getLogger("market_data.alpaca_stream")

_EQUITY_WS_URI = "wss://stream.data.alpaca.markets/v2/iex"
_CRYPTO_WS_URI = "wss://stream.data.alpaca.markets/v1beta3/crypto/us"

# If no message at all (data or control) arrives within this window, the
# connection is treated as dead even if the underlying TCP socket hasn't
# noticed yet — forces a reconnect rather than silently hanging forever.
HEARTBEAT_TIMEOUT_SECONDS = 60.0

# Reconnect backoff — capped exponential, same shape as every other
# retry/backoff choice in this codebase (e.g. AlpacaBroker's own rate-
# limit retry): fast the first time (transient blips are common), capped
# so a real outage doesn't hammer the endpoint.
_RECONNECT_BACKOFF_START_SECONDS = 1.0
_RECONNECT_BACKOFF_MAX_SECONDS = 30.0

EventType = Literal["trade", "quote", "bar"]


@dataclass(frozen=True)
class MarketEvent:
    event_type: EventType
    instrument: str
    time: datetime  # the exchange/feed's own timestamp
    received_at: datetime  # when THIS process saw it — for latency/staleness tracking
    price: float | None = None  # trade price, or bar close
    bid: float | None = None
    bid_size: float | None = None
    ask: float | None = None
    ask_size: float | None = None
    size: float | None = None  # trade size, or bar volume
    open: float | None = None
    high: float | None = None
    low: float | None = None


def _parse_time(raw: str) -> datetime:
    # Alpaca's own RFC3339 nanosecond-precision timestamps — same
    # truncate-to-microseconds fix src/broker/alpaca.py's parse_alpaca_time
    # already uses, duplicated here rather than imported to keep this
    # module's only dependency on src.broker.alpaca being none at all
    # (deliberate — see module docstring on why this never touches the
    # order-placing surface, not even via a shared import that happens to
    # live in the same file as place_order()).
    value = raw.replace("Z", "+00:00")
    if "." in value:
        head, rest = value.split(".", 1)
        frac, offset = rest[:-6], rest[-6:]
        value = f"{head}.{frac[:6]}{offset}"
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def _parse_message(raw: dict, received_at: datetime) -> MarketEvent | None:
    """None for control messages (success/subscription/error) or an
    unrecognized type — never raises on a shape this client doesn't know
    about yet, since a single malformed/novel message must not kill the
    whole stream."""
    t = raw.get("T")
    if t == "q":
        return MarketEvent(
            event_type="quote", instrument=raw["S"], time=_parse_time(raw["t"]), received_at=received_at,
            bid=raw.get("bp"), bid_size=raw.get("bs"), ask=raw.get("ap"), ask_size=raw.get("as"),
        )
    if t == "t":
        return MarketEvent(
            event_type="trade", instrument=raw["S"], time=_parse_time(raw["t"]), received_at=received_at,
            price=raw.get("p"), size=raw.get("s"),
        )
    if t == "b":
        return MarketEvent(
            event_type="bar", instrument=raw["S"], time=_parse_time(raw["t"]), received_at=received_at,
            open=raw.get("o"), high=raw.get("h"), low=raw.get("l"), price=raw.get("c"), size=raw.get("v"),
        )
    return None


class AlpacaMarketStream:
    """One instance per asset class (Alpaca uses separate WS hosts for
    equity vs. crypto feeds — confirmed live, see module docstring).
    listen() handles reconnect/re-subscribe/heartbeat/dedup internally —
    a caller just async-iterates it and gets a clean MarketEvent stream,
    indefinitely, until close() is called."""

    def __init__(
        self, settings: Settings, asset_class: Literal["equity", "crypto"],
        heartbeat_timeout_seconds: float = HEARTBEAT_TIMEOUT_SECONDS,
    ):
        self._settings = settings
        self._uri = _EQUITY_WS_URI if asset_class == "equity" else _CRYPTO_WS_URI
        self._heartbeat_timeout = heartbeat_timeout_seconds  # overridable so tests can fail fast, not production
        self._subscribed: dict[str, list[str]] = {"trades": [], "quotes": [], "bars": []}
        self._ws: websockets.ClientConnection | None = None
        self._last_event_at: dict[str, datetime] = {}
        self._last_seen_key: dict[tuple[str, str], tuple] = {}  # (instrument, type) -> dedup key
        self._closed = False

    async def _connect_and_auth(self) -> None:
        self._ws = await websockets.connect(self._uri)
        hello = json.loads(await self._ws.recv())
        if not (isinstance(hello, list) and hello and hello[0].get("msg") == "connected"):
            raise ConnectionError(f"Unexpected connect response: {hello!r}")
        await self._ws.send(json.dumps({
            "action": "auth", "key": self._settings.alpaca_api_key, "secret": self._settings.alpaca_api_secret,
        }))
        auth_resp = json.loads(await self._ws.recv())
        if not (isinstance(auth_resp, list) and auth_resp and auth_resp[0].get("msg") == "authenticated"):
            raise ConnectionError(f"Alpaca stream auth failed: {auth_resp!r}")
        if any(self._subscribed.values()):
            await self._ws.send(json.dumps({"action": "subscribe", **self._subscribed}))
            await self._ws.recv()  # the subscription-confirmation message

    async def subscribe(self, *, trades: list[str] = (), quotes: list[str] = (), bars: list[str] = ()) -> None:
        """Adds to whatever's already subscribed (not a replace) — matches
        Alpaca's own "subscribe" action semantics. Re-sent automatically
        on every reconnect via the tracked _subscribed state."""
        for key, symbols in (("trades", trades), ("quotes", quotes), ("bars", bars)):
            for s in symbols:
                if s not in self._subscribed[key]:
                    self._subscribed[key].append(s)
        if self._ws is not None and any((trades, quotes, bars)):
            await self._ws.send(json.dumps({
                "action": "subscribe", "trades": list(trades), "quotes": list(quotes), "bars": list(bars),
            }))
            await self._ws.recv()

    def is_stale(self, instrument: str, max_age_seconds: float = 30.0) -> bool:
        """True if no event for this instrument has arrived recently
        enough to trust — a caller (a future phase's feature engine) uses
        this to decide whether to fall back to AlpacaBroker.stream_prices()'s
        REST poll instead of trusting the stream."""
        last = self._last_event_at.get(instrument)
        if last is None:
            return True
        return (datetime.now(timezone.utc) - last).total_seconds() > max_age_seconds

    async def listen(self) -> AsyncIterator[MarketEvent]:
        """Runs forever (until close()) — reconnects on any drop with
        capped exponential backoff, re-authenticates, re-subscribes to
        everything subscribe() has ever been called with, and keeps
        yielding normalized events across reconnects transparently. A
        caller just does `async for event in stream.listen():`."""
        backoff = _RECONNECT_BACKOFF_START_SECONDS
        while not self._closed:
            try:
                if self._ws is None:
                    await self._connect_and_auth()
                    backoff = _RECONNECT_BACKOFF_START_SECONDS  # reset after a real success
                raw = await asyncio.wait_for(self._ws.recv(), timeout=self._heartbeat_timeout)
            except (TimeoutError, asyncio.TimeoutError):
                logger.warning("No message in %.0fs — treating connection as stale, reconnecting", self._heartbeat_timeout)
                await self._reset_connection()
                continue
            except (websockets.ConnectionClosed, ConnectionError, OSError) as exc:
                logger.warning("Stream connection lost (%r) — reconnecting in %.1fs", exc, backoff)
                await self._reset_connection()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _RECONNECT_BACKOFF_MAX_SECONDS)
                continue

            received_at = datetime.now(timezone.utc)
            try:
                messages = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Non-JSON message from stream, skipping: %r", raw[:200])
                continue

            for m in messages:
                event = _parse_message(m, received_at)
                if event is None:
                    continue
                dedup_key = (event.instrument, event.event_type)
                sig = (event.time, event.price, event.bid, event.ask, event.size)
                if self._last_seen_key.get(dedup_key) == sig:
                    continue  # exact duplicate of the last event for this instrument+type
                last_time = self._last_event_at.get(event.instrument)
                if last_time is not None and event.time < last_time:
                    continue  # out-of-order — older than what we've already processed
                self._last_seen_key[dedup_key] = sig
                self._last_event_at[event.instrument] = event.time
                yield event

    async def _reset_connection(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001 — closing an already-dead socket must never raise
                pass
            self._ws = None

    async def close(self) -> None:
        self._closed = True
        await self._reset_connection()

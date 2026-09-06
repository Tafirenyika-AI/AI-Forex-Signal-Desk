"""Alpaca broker adapter (US equities + crypto, paper trading only).

Implements the same BrokerAdapter interface OandaBroker does (src/broker/
base.py) so nothing downstream needs to know which broker it's talking to
— same method signatures, same "M15"/"H1" granularity vocabulary
(translated internally to Alpaca's own timeframe strings).

Two real API-shape gotchas found live-testing against the real paper
account (2026-08-21/22) that shaped this implementation:
  1. Alpaca's crypto POSITION symbols come back WITHOUT the slash
     ("BTCUSD"), while ORDER/QUOTE symbols use WITH the slash ("BTC/USD")
     — positions() re-inserts the slash so instrument strings stay
     consistent with src/broker/registry.py's crypto classifier ("/" in
     instrument) everywhere else in the system.
  2. Alpaca rejects order_class="bracket" for crypto outright (422
     "crypto orders not allowed for advanced order_class: otoco") — bracket
     (atomic entry + stop-loss + take-profit) only works for equities.
     Crypto entries submit as a plain market order, then — once filled —
     a genuine separate stop-loss order is placed for the actual filled
     quantity, so the risk governor's bounded-risk sizing assumption is
     honored for real (not faked). Take-profit is NOT attempted for
     crypto in this version: a non-atomic second exit order left racing
     against the stop would be worse than no take-profit at all (either
     could fill first, leaving the other as a naked, wrong-side order).
     This is a real, documented limitation, not silently pretended away.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterable
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import httpx

from src.broker.base import AccountState, BrokerAdapter, Candle, OrderResult, Price
from src.broker.registry import asset_class_for
from src.config import Settings

logger = logging.getLogger(__name__)

# Market data lives on a fixed host regardless of paper/live trading host —
# confirmed live: both the paper and (hypothetical) live trading accounts
# read market data from the same data.alpaca.markets endpoint.
ALPACA_DATA_HOST = "https://data.alpaca.markets"

MAX_CONCURRENT_REQUESTS = 8
RATE_LIMIT_RETRY_DELAY_SECONDS = 2.0

# M15/H1 are all src/run_loop.py's HORIZON_CONFIGS actually uses; H4/D
# added so the dashboard's Markets tab (free browsing at any granularity,
# not just the trading horizons) doesn't send OANDA's raw "H4"/"D" strings
# straight through to Alpaca's API, which doesn't recognize them.
_GRANULARITY_TO_ALPACA = {"M15": "15Min", "H1": "1Hour", "H4": "4Hour", "D": "1Day"}
_GRANULARITY_MINUTES = {"M15": 15, "H1": 60, "H4": 240, "D": 1440}

# How long to poll a just-submitted crypto market order for its fill before
# giving up on attaching a protective stop — crypto market orders on
# Alpaca's paper API filled within ~1s in live testing, this gives real
# headroom without blocking a decision cycle for long.
CRYPTO_FILL_POLL_ATTEMPTS = 10
CRYPTO_FILL_POLL_DELAY_SECONDS = 0.5


class AlpacaApiError(RuntimeError):
    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self.body = body
        message = body.get("message") if isinstance(body, dict) else body
        super().__init__(f"Alpaca API error {status_code}: {message}")


def parse_alpaca_time(value: str) -> datetime:
    # Alpaca RFC3339 timestamps look like "2026-08-22T00:15:50.274641243Z" —
    # same nanosecond-precision issue OANDA's parser handles, same fix.
    value = value.replace("Z", "+00:00")
    if "." in value:
        head, rest = value.split(".", 1)
        frac, offset = rest[:-6], rest[-6:]
        value = f"{head}.{frac[:6]}{offset}"
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def _denormalize_crypto_symbol(symbol: str) -> str:
    """positions() gives crypto symbols without a slash ("BTCUSD") —
    re-insert it so this stays consistent with registry.asset_class_for's
    "/" in instrument crypto check. Only crypto symbols lack the slash to
    begin with (equities are never split like this), and all of this
    account's crypto pairs quote in USD, so the split point is unambiguous."""
    if "/" in symbol or not symbol.endswith("USD") or len(symbol) <= 3:
        return symbol
    return f"{symbol[:-3]}/USD"


class AlpacaBroker(BrokerAdapter):
    def __init__(self, settings: Settings):
        self._settings = settings
        self._headers = {
            "APCA-API-KEY-ID": settings.alpaca_api_key,
            "APCA-API-SECRET-KEY": settings.alpaca_api_secret,
            "Content-Type": "application/json",
        }
        self._trading_client = httpx.AsyncClient(
            base_url=settings.alpaca_base_url, headers=self._headers, timeout=15.0,
        )
        self._data_client = httpx.AsyncClient(
            base_url=ALPACA_DATA_HOST, headers=self._headers, timeout=15.0,
        )
        self._request_semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        self._instrument_cache: dict[str, dict[str, Any]] | None = None

    async def close(self) -> None:
        await self._trading_client.aclose()
        await self._data_client.aclose()

    async def __aenter__(self) -> "AlpacaBroker":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def _request(self, client: httpx.AsyncClient, method: str, path: str, **kwargs: Any) -> Any:
        async with self._request_semaphore:
            response = await client.request(method, path, **kwargs)
            if response.status_code == 429:
                logger.warning("Alpaca rate limit hit on %s %s — retrying once after %.1fs",
                                method, path, RATE_LIMIT_RETRY_DELAY_SECONDS)
                await asyncio.sleep(RATE_LIMIT_RETRY_DELAY_SECONDS)
                response = await client.request(method, path, **kwargs)
            if response.status_code >= 400:
                try:
                    body = response.json()
                except ValueError:
                    body = response.text
                raise AlpacaApiError(response.status_code, body)
        if not response.content:
            return {}
        return response.json()

    async def list_instruments(self) -> dict[str, dict[str, Any]]:
        """{symbol: {...Alpaca's own asset metadata: class, exchange,
        tradable, fractionable...}} across both tradable US equities and
        active crypto pairs — cached in-process like OandaBroker does."""
        if self._instrument_cache is not None:
            return self._instrument_cache
        equities = await self._request(
            self._trading_client, "GET", "/assets",
            params={"status": "active", "asset_class": "us_equity"},
        )
        crypto = await self._request(
            self._trading_client, "GET", "/assets",
            params={"status": "active", "asset_class": "crypto"},
        )
        cache: dict[str, dict[str, Any]] = {}
        for a in equities + crypto:
            if a.get("tradable"):
                cache[a["symbol"]] = a
        self._instrument_cache = cache
        return cache

    async def account_state(self) -> AccountState:
        acc = await self._request(self._trading_client, "GET", "/account")
        positions = await self.positions()
        unrealized_pl = sum(float(p.get("unrealized_pl", 0.0)) for p in positions)
        return AccountState(
            account_id=acc["account_number"],
            currency=acc["currency"],
            balance=float(acc["cash"]),
            nav=float(acc["equity"]),
            unrealized_pl=unrealized_pl,
            # Alpaca has no direct "margin used/available" pair like OANDA —
            # initial_margin/buying_power are the closest real analogs.
            margin_used=float(acc.get("initial_margin", 0.0) or 0.0),
            margin_available=float(acc.get("buying_power", 0.0) or 0.0),
            open_trade_count=len(positions),
            open_position_count=len(positions),
        )

    async def get_current_prices(self, instruments: Iterable[str]) -> list[Price]:
        instruments = list(instruments)
        equity_syms = [i for i in instruments if asset_class_for(i) == "equity"]
        crypto_syms = [i for i in instruments if asset_class_for(i) == "crypto"]
        prices: list[Price] = []

        if equity_syms:
            data = await self._request(
                self._data_client, "GET", "/v2/stocks/quotes/latest",
                params={"symbols": ",".join(equity_syms)},
            )
            for sym, q in data.get("quotes", {}).items():
                if not q.get("bp") or not q.get("ap"):
                    continue
                prices.append(Price(instrument=sym, time=parse_alpaca_time(q["t"]), bid=float(q["bp"]), ask=float(q["ap"])))

        if crypto_syms:
            data = await self._request(
                self._data_client, "GET", "/v1beta3/crypto/us/latest/quotes",
                params={"symbols": ",".join(crypto_syms)},
            )
            for sym, q in data.get("quotes", {}).items():
                prices.append(Price(instrument=sym, time=parse_alpaca_time(q["t"]), bid=float(q["bp"]), ask=float(q["ap"])))

        return prices

    async def stream_prices(self, instruments: Iterable[str]) -> AsyncIterator[Price]:
        """No websocket client here (nothing in this codebase's real
        decision cycle holds a live stream open — see src/models/
        market_reaction.py's docstring on why every data source here is
        polled, not streamed). This is a genuine, working implementation
        of the ABC's contract via repeated polling, not a stub — just a
        different transport than a true push stream."""
        instruments = list(instruments)
        while True:
            for p in await self.get_current_prices(instruments):
                yield p
            await asyncio.sleep(5.0)

    async def get_candles(self, instrument: str, granularity: str, count: int = 500) -> list[Candle]:
        return await self._get_candles(instrument, granularity, count=count)

    async def get_candles_range(
        self, instrument: str, granularity: str, from_time: datetime, to_time: datetime,
    ) -> list[Candle]:
        return await self._get_candles(instrument, granularity, start=from_time, end=to_time)

    async def _get_candles(
        self, instrument: str, granularity: str, count: int | None = None,
        start: datetime | None = None, end: datetime | None = None,
    ) -> list[Candle]:
        timeframe = _GRANULARITY_TO_ALPACA.get(granularity, granularity)
        now = datetime.now(timezone.utc)
        granularity_minutes = _GRANULARITY_MINUTES.get(granularity, 15)

        # Real gap found live 2026-08-22: a count-only request (no explicit
        # start/end) at H4/D granularity came back {"bars": null} from
        # Alpaca — a start date DOES return real data, so this isn't "no
        # history exists," just that Alpaca's own "recent window" default
        # doesn't reliably cover N bars at the coarser granularities. A
        # generous explicit start (2x the nominal window) makes a
        # count-only call behave the way callers actually expect: "the
        # last ~count bars," not "whatever Alpaca's undocumented default
        # window happens to include."
        if count is not None and start is None:
            start = now - timedelta(minutes=granularity_minutes * count * 2)

        params: dict[str, Any] = {"timeframe": timeframe}
        if count is not None:
            params["limit"] = count
        if start is not None:
            params["start"] = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        if end is not None:
            params["end"] = end.strftime("%Y-%m-%dT%H:%M:%SZ")

        # A count-bounded live-cycle call (limit <= TRAIN_CANDLE_COUNT) fits
        # in one page and this loop exits immediately; a range-bounded
        # backfill call (src/scripts/backfill_candles.py) can span years and
        # needs every page — Alpaca's bars endpoints truncate silently at
        # their own per-page cap (1000 bars) and hand back a
        # "next_page_token" instead of erroring, so a caller that doesn't
        # follow it just gets an incomplete result with no signal anything
        # was cut off.
        is_crypto = asset_class_for(instrument) == "crypto"
        raw_bars: list[dict] = []
        page_token: str | None = None
        while True:
            page_params = dict(params)
            if page_token:
                page_params["page_token"] = page_token
            if is_crypto:
                data = await self._request(
                    self._data_client, "GET", "/v1beta3/crypto/us/bars",
                    params={**page_params, "symbols": instrument},
                )
                # .get(..., {}) still needs "or {}" — Alpaca returns a
                # literal {"bars": null} rather than {} for a symbol with
                # nothing in range, same quirk as the equities endpoint below.
                raw_bars.extend((data.get("bars") or {}).get(instrument) or [])
            else:
                # Real gap found live: this account's Alpaca subscription
                # tier rejects recent-data requests against the default SIP
                # (consolidated tape) feed with a 403 ("subscription does
                # not permit querying recent SIP data") — feed="iex" uses
                # IEX-only data instead, which the free/paper tier can
                # access without restriction.
                data = await self._request(
                    self._data_client, "GET", f"/v2/stocks/{instrument}/bars",
                    params={**page_params, "feed": "iex"},
                )
                raw_bars.extend(data.get("bars") or [])
            page_token = data.get("next_page_token")
            if not page_token:
                break

        candles = []
        for b in raw_bars:
            bar_time = parse_alpaca_time(b["t"])
            complete = (bar_time + timedelta(minutes=granularity_minutes)) <= now
            candles.append(Candle(
                instrument=instrument, granularity=granularity, time=bar_time,
                open=float(b["o"]), high=float(b["h"]), low=float(b["l"]), close=float(b["c"]),
                volume=int(b.get("v", 0)), complete=complete,
            ))
        return candles

    async def place_order(
        self,
        instrument: str,
        units: int | float,
        client_order_id: str,
        stop_loss_price: float | None = None,
        take_profit_price: float | None = None,
        order_type: Literal["MARKET", "LIMIT"] = "MARKET",
        limit_price: float | None = None,
    ) -> OrderResult:
        side = "buy" if units > 0 else "sell"
        qty = abs(units)
        is_crypto = asset_class_for(instrument) == "crypto"

        if is_crypto:
            # See module docstring — Alpaca rejects bracket orders for
            # crypto outright, so the entry always goes out as a plain
            # order here; protective stop is attached separately below,
            # only once the fill is confirmed for real.
            body: dict[str, Any] = {
                "symbol": instrument, "qty": str(qty), "side": side,
                "type": "market" if order_type == "MARKET" else "limit",
                "time_in_force": "gtc",  # crypto doesn't support "day"
                "client_order_id": client_order_id,
            }
            if order_type == "LIMIT":
                if limit_price is None:
                    raise ValueError("limit_price is required for LIMIT orders")
                body["limit_price"] = f"{limit_price:.2f}"
        else:
            body = {
                "symbol": instrument, "qty": str(qty), "side": side,
                "type": "market" if order_type == "MARKET" else "limit",
                "time_in_force": "day",
                "client_order_id": client_order_id,
            }
            if order_type == "LIMIT":
                if limit_price is None:
                    raise ValueError("limit_price is required for LIMIT orders")
                body["limit_price"] = f"{limit_price:.2f}"
            if stop_loss_price is not None or take_profit_price is not None:
                body["order_class"] = "bracket"
                if take_profit_price is not None:
                    body["take_profit"] = {"limit_price": f"{take_profit_price:.2f}"}
                if stop_loss_price is not None:
                    body["stop_loss"] = {"stop_price": f"{stop_loss_price:.2f}"}

        data = await self._request(self._trading_client, "POST", "/orders", json=body)
        result = OrderResult(
            client_order_id=client_order_id,
            broker_order_id=data.get("id"),
            broker_transaction_id=data.get("id"),
            status=str(data.get("status", "unknown")),
            raw=data,
        )

        if is_crypto and stop_loss_price is not None:
            await self._attach_crypto_stop_loss(instrument, data["id"], side, stop_loss_price, client_order_id)

        return result

    async def _attach_crypto_stop_loss(
        self, instrument: str, entry_order_id: str, entry_side: str, stop_loss_price: float, client_order_id: str,
    ) -> None:
        """Polls the just-submitted crypto entry order for its real fill,
        then places a genuine separate stop order for the actual filled
        quantity — never assumes the requested qty filled exactly as asked.
        Gives up (logs, doesn't raise — the entry itself already succeeded)
        if it doesn't fill within the poll window; the position exists
        without a broker-side stop in that case, a real gap surfaced in
        logs rather than silently pretended protected."""
        filled_qty = None
        for _ in range(CRYPTO_FILL_POLL_ATTEMPTS):
            await asyncio.sleep(CRYPTO_FILL_POLL_DELAY_SECONDS)
            order = await self._request(self._trading_client, "GET", f"/orders/{entry_order_id}")
            if order.get("status") == "filled":
                # Real gap found live-testing: the order's own filled_qty
                # is the gross fill amount, but Alpaca deducts crypto fees
                # from the position itself, so the position's qty_available
                # ends up slightly less (e.g. 0.00029925 vs a requested/
                # filled 0.0003) — sizing the stop order off filled_qty
                # directly then gets rejected with "insufficient balance".
                # qty_available is what can actually be sold.
                symbol_no_slash = instrument.replace("/", "")
                position = await self._request(self._trading_client, "GET", f"/positions/{symbol_no_slash}")
                filled_qty = float(position["qty_available"])
                break
        if not filled_qty:
            logger.warning(
                "Alpaca crypto entry %s (%s) did not fill within %.1fs — no stop-loss attached, "
                "position (if any) is currently unprotected at the broker",
                entry_order_id, instrument, CRYPTO_FILL_POLL_ATTEMPTS * CRYPTO_FILL_POLL_DELAY_SECONDS,
            )
            return

        # Real gap found live-testing: Alpaca rejects a plain type="stop"
        # order for crypto ("invalid order type for crypto order", 422) —
        # crypto only supports stop_limit. A stop_limit needs a limit_price
        # too, or the order could trigger and then never fill in a fast
        # move; a 1% buffer past the stop keeps it marketable without
        # meaningfully changing the effective stop level.
        exit_side = "sell" if entry_side == "buy" else "buy"
        limit_buffer = stop_loss_price * 0.01
        limit_price = stop_loss_price - limit_buffer if exit_side == "sell" else stop_loss_price + limit_buffer
        stop_body = {
            "symbol": instrument, "qty": str(filled_qty), "side": exit_side,
            "type": "stop_limit", "time_in_force": "gtc",
            "stop_price": f"{stop_loss_price:.2f}", "limit_price": f"{limit_price:.2f}",
            "client_order_id": f"{client_order_id}-stop",
        }
        await self._request(self._trading_client, "POST", "/orders", json=stop_body)

    async def cancel_order(self, broker_order_id: str) -> None:
        await self._request(self._trading_client, "DELETE", f"/orders/{broker_order_id}")

    async def get_order(self, broker_order_id: str) -> dict[str, Any]:
        """Single-order fetch — unlike transactions()'s bulk closed-orders
        list, this one nests a bracket order's take-profit/stop-loss legs
        with their own live status (real gap found live 2026-08-22: the
        bulk list flattens legs into separate top-level entries instead,
        so it can't answer "did this specific bracket's exit fill yet").
        Used by src/outcomes/alpaca_tracker.py."""
        return await self._request(self._trading_client, "GET", f"/orders/{broker_order_id}")

    async def positions(self) -> list[dict[str, Any]]:
        data = await self._request(self._trading_client, "GET", "/positions")
        for p in data:
            if p.get("asset_class") == "crypto":
                p["symbol"] = _denormalize_crypto_symbol(p["symbol"])
        return data

    async def transactions(self, since_id: str | None = None) -> list[dict[str, Any]]:
        """Alpaca has no OANDA-style transaction ledger — closed/filled
        orders are the closest equivalent. Bracket legs come back as
        separate flattened top-level entries here, not nested under their
        parent (see get_order()'s docstring) — src/outcomes/
        alpaca_tracker.py uses this only to find a crypto stop-loss order
        by client_order_id, and get_order() for everything that needs a
        specific order's real state."""
        params: dict[str, Any] = {"status": "closed", "direction": "desc", "limit": 500}
        if since_id:
            params["after"] = since_id
        return await self._request(self._trading_client, "GET", "/orders", params=params)

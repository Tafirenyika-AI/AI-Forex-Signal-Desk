from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Iterable
from datetime import datetime, timezone
from typing import Any, Literal

import httpx

from src.broker.base import AccountState, BrokerAdapter, Candle, OrderResult, Price
from src.config import Settings

logger = logging.getLogger(__name__)

# Real rate-limiting risk once instrument count grows beyond the original 5
# pairs (verified live 2026-08-13: this account alone has 68 tradable FX
# pairs) — a naive fan-out of concurrent requests across dozens of
# instruments could trip OANDA's practice-API rate limits, which were
# previously masked by accident at 5 pairs, not by design. A semaphore caps
# real concurrency; a 429 gets one retry with backoff rather than a hard
# failure for what's usually a transient, self-resolving condition.
MAX_CONCURRENT_REQUESTS = 8
RATE_LIMIT_RETRY_DELAY_SECONDS = 2.0


class OandaApiError(RuntimeError):
    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self.body = body
        message = body.get("errorMessage") if isinstance(body, dict) else body
        super().__init__(f"OANDA API error {status_code}: {message}")


def parse_oanda_time(value: str) -> datetime:
    # OANDA RFC3339 timestamps look like "2026-08-07T18:31:04.123456789Z"
    value = value.replace("Z", "+00:00")
    # Python's fromisoformat can't handle nanosecond precision; truncate to microseconds.
    if "." in value:
        head, rest = value.split(".", 1)
        frac, offset = rest[:-6], rest[-6:]
        value = f"{head}.{frac[:6]}{offset}"
    return datetime.fromisoformat(value).astimezone(timezone.utc)


class OandaBroker(BrokerAdapter):
    def __init__(self, settings: Settings):
        self._settings = settings
        self._headers = {
            "Authorization": f"Bearer {settings.oanda_api_token}",
            "Content-Type": "application/json",
        }
        self._rest_client = httpx.AsyncClient(
            base_url=settings.oanda_rest_host,
            headers=self._headers,
            timeout=15.0,
        )
        self._account_id = settings.oanda_account_id or None
        self._request_semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        self._instrument_cache: dict[str, dict[str, Any]] | None = None

    async def close(self) -> None:
        await self._rest_client.aclose()

    async def __aenter__(self) -> "OandaBroker":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        async with self._request_semaphore:
            response = await self._rest_client.request(method, path, **kwargs)
            if response.status_code == 429:
                # One retry after a short backoff — practice-API rate limits
                # are usually a brief, self-resolving condition at this
                # request volume, not a sign something is fundamentally wrong.
                logger.warning("OANDA rate limit hit on %s %s — retrying once after %.1fs",
                                method, path, RATE_LIMIT_RETRY_DELAY_SECONDS)
                await asyncio.sleep(RATE_LIMIT_RETRY_DELAY_SECONDS)
                response = await self._rest_client.request(method, path, **kwargs)
            if response.status_code >= 400:
                try:
                    body = response.json()
                except ValueError:
                    body = response.text
                raise OandaApiError(response.status_code, body)
        if not response.content:
            return {}
        return response.json()

    async def list_instruments(self) -> dict[str, dict[str, Any]]:
        """Every instrument this account's own token can actually trade —
        verified live 2026-08-13 that this varies by account (this system's
        admin account has 68 CURRENCY-type pairs, no indices/commodities/
        metals), so this is queried per-user against their own credentials
        rather than assumed to be one fixed universe. Cached in-process
        since it changes rarely within one broker instance's lifetime.
        Returns {instrument_name: {...OANDA's own metadata: type,
        pipLocation, displayPrecision, marginRate, minimumTradeSize,
        maximumOrderUnits...}}."""
        if self._instrument_cache is not None:
            return self._instrument_cache
        account_id = await self.resolve_account_id()
        data = await self._request("GET", f"/v3/accounts/{account_id}/instruments")
        self._instrument_cache = {i["name"]: i for i in data.get("instruments", [])}
        return self._instrument_cache

    async def list_accounts(self) -> list[str]:
        data = await self._request("GET", "/v3/accounts")
        return [acc["id"] for acc in data.get("accounts", [])]

    async def resolve_account_id(self) -> str:
        if self._account_id:
            return self._account_id
        accounts = await self.list_accounts()
        if not accounts:
            raise RuntimeError("OANDA token has no accounts associated with it.")
        self._account_id = accounts[0]
        logger.info("Resolved OANDA account id: %s", self._account_id)
        return self._account_id

    async def account_state(self) -> AccountState:
        account_id = await self.resolve_account_id()
        data = await self._request("GET", f"/v3/accounts/{account_id}/summary")
        acc = data["account"]
        return AccountState(
            account_id=acc["id"],
            currency=acc["currency"],
            balance=float(acc["balance"]),
            nav=float(acc["NAV"]),
            unrealized_pl=float(acc["unrealizedPL"]),
            margin_used=float(acc["marginUsed"]),
            margin_available=float(acc["marginAvailable"]),
            open_trade_count=int(acc["openTradeCount"]),
            open_position_count=int(acc["openPositionCount"]),
        )

    async def get_current_prices(self, instruments: Iterable[str]) -> list[Price]:
        account_id = await self.resolve_account_id()
        params = {"instruments": ",".join(instruments)}
        data = await self._request(
            "GET", f"/v3/accounts/{account_id}/pricing", params=params
        )
        prices = []
        for p in data.get("prices", []):
            bids = p.get("bids") or p.get("closeoutBid")
            asks = p.get("asks") or p.get("closeoutAsk")
            bid = float(bids[0]["price"]) if isinstance(bids, list) else float(p["closeoutBid"])
            ask = float(asks[0]["price"]) if isinstance(asks, list) else float(p["closeoutAsk"])
            prices.append(
                Price(
                    instrument=p["instrument"],
                    time=parse_oanda_time(p["time"]),
                    bid=bid,
                    ask=ask,
                )
            )
        return prices

    async def stream_prices(self, instruments: Iterable[str]) -> AsyncIterator[Price]:
        account_id = await self.resolve_account_id()
        url = f"{self._settings.oanda_stream_host}/v3/accounts/{account_id}/pricing/stream"
        params = {"instruments": ",".join(instruments)}
        async with httpx.AsyncClient(headers=self._headers, timeout=None) as client:
            async with client.stream("GET", url, params=params) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    raise OandaApiError(response.status_code, body.decode())
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    msg = json.loads(line)
                    if msg.get("type") == "HEARTBEAT":
                        continue
                    if msg.get("type") == "PRICE":
                        yield Price(
                            instrument=msg["instrument"],
                            time=parse_oanda_time(msg["time"]),
                            bid=float(msg["closeoutBid"]),
                            ask=float(msg["closeoutAsk"]),
                        )

    async def get_candles(
        self,
        instrument: str,
        granularity: str,
        count: int = 500,
    ) -> list[Candle]:
        params = {"granularity": granularity, "count": count, "price": "M"}
        data = await self._request(
            "GET", f"/v3/instruments/{instrument}/candles", params=params
        )
        return self._parse_candles(instrument, granularity, data)

    async def get_candles_range(
        self,
        instrument: str,
        granularity: str,
        from_time: datetime,
        to_time: datetime,
    ) -> list[Candle]:
        """Historical candles bracketing a specific window — OANDA retains
        data back to 2005 (per their own docs), so this works for any past
        event time, not just recent ones. Used by src/models/market_reaction.py
        to measure actual price movement around a scheduled release."""
        params = {
            "granularity": granularity,
            "from": from_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "to": to_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "price": "M",
        }
        data = await self._request(
            "GET", f"/v3/instruments/{instrument}/candles", params=params
        )
        return self._parse_candles(instrument, granularity, data)

    @staticmethod
    def _parse_candles(instrument: str, granularity: str, data: dict[str, Any]) -> list[Candle]:
        candles = []
        for c in data.get("candles", []):
            mid = c["mid"]
            candles.append(
                Candle(
                    instrument=instrument,
                    granularity=granularity,
                    time=parse_oanda_time(c["time"]),
                    open=float(mid["o"]),
                    high=float(mid["h"]),
                    low=float(mid["l"]),
                    close=float(mid["c"]),
                    volume=int(c["volume"]),
                    complete=bool(c["complete"]),
                )
            )
        return candles

    async def _format_price(self, instrument: str, price: float) -> str:
        """OANDA rejects the whole order (400, "more precision than is
        allowed by the Order's instrument") if a price has more decimal
        places than that specific instrument's own displayPrecision — this
        varies per instrument (verified live 2026-08-13: JPY crosses and
        USD_HUF are 3 decimals, most other FX pairs are 5), so a single
        hardcoded format string is wrong for a large share of a real
        68-instrument universe. Falls back to 5dp only if this instrument
        is somehow missing from list_instruments() (shouldn't happen for
        anything this account can actually trade)."""
        instruments = await self.list_instruments()
        precision = instruments.get(instrument, {}).get("displayPrecision", 5)
        return f"{price:.{precision}f}"

    async def place_order(
        self,
        instrument: str,
        units: int,
        client_order_id: str,
        stop_loss_price: float | None = None,
        take_profit_price: float | None = None,
        order_type: Literal["MARKET", "LIMIT"] = "MARKET",
        limit_price: float | None = None,
    ) -> OrderResult:
        account_id = await self.resolve_account_id()

        order: dict[str, Any] = {
            "type": order_type,
            "instrument": instrument,
            "units": str(units),
            "positionFill": "DEFAULT",
            "clientExtensions": {"id": client_order_id},
        }
        if order_type == "MARKET":
            order["timeInForce"] = "FOK"
        elif order_type == "LIMIT":
            if limit_price is None:
                raise ValueError("limit_price is required for LIMIT orders")
            order["timeInForce"] = "GTC"
            order["price"] = await self._format_price(instrument, limit_price)

        if stop_loss_price is not None:
            order["stopLossOnFill"] = {"price": await self._format_price(instrument, stop_loss_price)}
        if take_profit_price is not None:
            order["takeProfitOnFill"] = {"price": await self._format_price(instrument, take_profit_price)}

        data = await self._request(
            "POST", f"/v3/accounts/{account_id}/orders", json={"order": order}
        )

        fill = data.get("orderFillTransaction")
        create = data.get("orderCreateTransaction")
        cancel = data.get("orderCancelTransaction")

        if fill:
            status = "FILLED"
            broker_transaction_id = fill.get("id")
        elif cancel:
            status = f"CANCELLED:{cancel.get('reason')}"
            broker_transaction_id = cancel.get("id")
        else:
            status = "PENDING"
            broker_transaction_id = create.get("id") if create else None

        return OrderResult(
            client_order_id=client_order_id,
            broker_order_id=create.get("id") if create else None,
            broker_transaction_id=broker_transaction_id,
            status=status,
            raw=data,
        )

    async def cancel_order(self, broker_order_id: str) -> None:
        account_id = await self.resolve_account_id()
        await self._request(
            "PUT", f"/v3/accounts/{account_id}/orders/{broker_order_id}/cancel"
        )

    async def positions(self) -> list[dict[str, Any]]:
        account_id = await self.resolve_account_id()
        data = await self._request("GET", f"/v3/accounts/{account_id}/openPositions")
        return data.get("positions", [])

    async def transactions(self, since_id: str | None = None) -> list[dict[str, Any]]:
        """Full transaction objects, not the paginated summary. Verified
        live (2026-08-13): the bare `GET /transactions` endpoint returns
        `{"pages": [...], "count": N, ...}` with NO inline "transactions"
        key at all — calling it without since_id used to silently return []
        forever, hiding real trading activity. `/transactions/sinceid` (and
        `/idrange`, used below) are the endpoints that actually return
        transaction objects inline."""
        account_id = await self.resolve_account_id()
        if since_id:
            data = await self._request(
                "GET",
                f"/v3/accounts/{account_id}/transactions/sinceid",
                params={"id": since_id},
            )
            return data.get("transactions", [])

        summary = await self._request("GET", f"/v3/accounts/{account_id}/transactions")
        last_id = int(summary.get("lastTransactionID", 0))
        if last_id == 0:
            return []
        data = await self._request(
            "GET",
            f"/v3/accounts/{account_id}/transactions/idrange",
            params={"from": 1, "to": last_id},
        )
        return data.get("transactions", [])

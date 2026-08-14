from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal


@dataclass(frozen=True)
class Price:
    instrument: str
    time: datetime
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass(frozen=True)
class Candle:
    instrument: str
    granularity: str
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    complete: bool


@dataclass(frozen=True)
class AccountState:
    account_id: str
    currency: str
    balance: float
    nav: float
    unrealized_pl: float
    margin_used: float
    margin_available: float
    open_trade_count: int
    open_position_count: int


@dataclass(frozen=True)
class OrderResult:
    client_order_id: str
    broker_order_id: str | None
    broker_transaction_id: str | None
    status: str
    raw: dict[str, Any]


class BrokerAdapter(ABC):
    """Normalized interface so the decision system never touches broker-specific objects."""

    @abstractmethod
    async def account_state(self) -> AccountState: ...

    @abstractmethod
    async def get_current_prices(self, instruments: Iterable[str]) -> list[Price]:
        ...

    @abstractmethod
    def stream_prices(self, instruments: Iterable[str]) -> AsyncIterator[Price]:
        ...

    @abstractmethod
    async def get_candles(
        self,
        instrument: str,
        granularity: str,
        count: int = 500,
    ) -> list[Candle]:
        ...

    @abstractmethod
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
        ...

    @abstractmethod
    async def cancel_order(self, broker_order_id: str) -> None: ...

    @abstractmethod
    async def positions(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def transactions(self, since_id: str | None = None) -> list[dict[str, Any]]: ...

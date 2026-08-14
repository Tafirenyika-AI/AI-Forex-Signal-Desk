"""Paper broker (blueprint sec. 1 "Initial mode: Paper/demo only", sec. 14 Phase 3-4).

Reuses OandaBroker for all read paths (real live/demo prices, candles,
streaming) so paper fills are priced against genuine market data — only
order placement is simulated, paying the real bid/ask spread.

The ledger (positions, realized P/L, seen order IDs) is persisted in the
database, not kept in process memory. That's not just for durability across
restarts — it's required correctness: the dashboard issues one short-lived
`asyncio.run(...)` per button click, each with its own event loop, so a
long-lived in-memory broker object (and its httpx connections, bound to
whichever loop created them) can't safely be reused across clicks. Treating
the DB as the source of truth sidesteps that entirely: every call opens
fresh, reads current state, and writes back atomically.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Engine

from src.broker.base import AccountState, OrderResult
from src.broker.oanda import OandaBroker
from src.config import Settings
from src.data.db import paper_account_state, paper_order_ids_seen, paper_positions
from src.risk.governor import usd_value_per_unit


class PaperBroker:
    def __init__(self, settings: Settings, engine: Engine, user_id: int, starting_balance: float = 100_000.0):
        self.settings = settings
        self.engine = engine
        self.user_id = user_id
        self.starting_balance = starting_balance
        self._price_source = OandaBroker(settings)

    async def close(self) -> None:
        await self._price_source.close()

    async def __aenter__(self) -> "PaperBroker":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def get_current_prices(self, instruments):
        return await self._price_source.get_current_prices(instruments)

    async def get_candles(self, instrument: str, granularity: str, count: int = 500):
        return await self._price_source.get_candles(instrument, granularity, count)

    def stream_prices(self, instruments):
        return self._price_source.stream_prices(instruments)

    def _ensure_account_row(self, conn) -> dict:
        row = conn.execute(
            select(paper_account_state).where(paper_account_state.c.user_id == self.user_id)
        ).mappings().first()
        if row:
            return dict(row)
        stmt = insert(paper_account_state).values(
            user_id=self.user_id,
            starting_balance=self.starting_balance,
            realized_pl=0.0,
            updated_at=datetime.now(timezone.utc),
        )
        conn.execute(stmt)
        return {"user_id": self.user_id, "starting_balance": self.starting_balance, "realized_pl": 0.0}

    def _open_positions(self, conn) -> dict[str, dict]:
        rows = conn.execute(
            select(paper_positions).where(paper_positions.c.user_id == self.user_id)
        ).mappings().all()
        return {r["instrument"]: dict(r) for r in rows if r["units"] != 0}

    async def account_state(self) -> AccountState:
        with self.engine.begin() as conn:
            account_row = self._ensure_account_row(conn)
            open_positions = self._open_positions(conn)

        unrealized = 0.0
        for instrument, pos in open_positions.items():
            prices = await self.get_current_prices([instrument])
            if not prices:
                continue
            mid = prices[0].mid
            # price diff is in quote-currency terms; convert to USD (account
            # currency) — for USD_JPY/USD_CAD that's not a 1:1 conversion.
            unrealized += pos["units"] * (mid - pos["avg_price"]) * usd_value_per_unit(instrument, mid)

        balance = account_row["starting_balance"] + account_row["realized_pl"]
        nav = balance + unrealized
        open_count = len(open_positions)
        return AccountState(
            account_id="PAPER",
            currency="USD",
            balance=balance,
            nav=nav,
            unrealized_pl=unrealized,
            margin_used=0.0,
            margin_available=nav,
            open_trade_count=open_count,
            open_position_count=open_count,
        )

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
        with self.engine.connect() as conn:
            already_seen = conn.execute(
                select(paper_order_ids_seen).where(paper_order_ids_seen.c.client_order_id == client_order_id)
            ).first()
        if already_seen:
            # duplicate gate, sec. 7: idempotent no-op on retry, not a second fill
            return OrderResult(client_order_id, None, None, "DUPLICATE_IGNORED", {})

        prices = await self.get_current_prices([instrument])
        if not prices:
            return OrderResult(client_order_id, None, None, "REJECTED_NO_PRICE", {})
        price = prices[0]
        fill_price = price.ask if units > 0 else price.bid  # you pay the spread, always
        now = datetime.now(timezone.utc)

        with self.engine.begin() as conn:
            account_row = self._ensure_account_row(conn)
            pos_row = conn.execute(
                select(paper_positions).where(
                    paper_positions.c.user_id == self.user_id, paper_positions.c.instrument == instrument
                )
            ).mappings().first()
            pos_units = pos_row["units"] if pos_row else 0
            pos_avg_price = pos_row["avg_price"] if pos_row else 0.0
            realized_pl_delta = 0.0

            if pos_units == 0 or (pos_units > 0) == (units > 0):
                # opening or adding to a position in the same direction
                new_units = pos_units + units
                new_avg_price = (
                    (pos_avg_price * pos_units + fill_price * units) / new_units
                    if new_units != 0
                    else fill_price
                )
            else:
                # reducing or flipping
                closing_units = min(abs(units), abs(pos_units))
                direction = 1 if pos_units > 0 else -1
                realized_pl_delta = (
                    direction * closing_units * (fill_price - pos_avg_price)
                    * usd_value_per_unit(instrument, fill_price)
                )
                new_units = pos_units + units
                new_avg_price = fill_price if (new_units > 0) != (direction > 0) and new_units != 0 else pos_avg_price

            upsert_pos = insert(paper_positions).values(
                user_id=self.user_id, instrument=instrument, units=new_units, avg_price=new_avg_price, updated_at=now,
            )
            upsert_pos = upsert_pos.on_conflict_do_update(
                index_elements=["user_id", "instrument"],
                set_={"units": new_units, "avg_price": new_avg_price, "updated_at": now},
            )
            conn.execute(upsert_pos)

            if realized_pl_delta:
                upsert_account = insert(paper_account_state).values(
                    user_id=self.user_id,
                    starting_balance=self.starting_balance,
                    realized_pl=account_row["realized_pl"] + realized_pl_delta,
                    updated_at=now,
                )
                upsert_account = upsert_account.on_conflict_do_update(
                    index_elements=["user_id"],
                    set_={"realized_pl": account_row["realized_pl"] + realized_pl_delta, "updated_at": now},
                )
                conn.execute(upsert_account)

            conn.execute(insert(paper_order_ids_seen).values(
                client_order_id=client_order_id, user_id=self.user_id, seen_at=now,
            ))

        return OrderResult(
            client_order_id=client_order_id,
            broker_order_id=f"PAPER-{client_order_id}",
            broker_transaction_id=f"PAPER-{client_order_id}",
            status="FILLED",
            raw={
                "fill_price": fill_price,
                "spread": price.spread,
                "stop_loss_price": stop_loss_price,
                "take_profit_price": take_profit_price,
                "time": now.isoformat(),
            },
        )

    async def cancel_order(self, broker_order_id: str) -> None:
        return None  # paper market orders fill immediately; nothing pending to cancel

    async def positions(self) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            open_positions = self._open_positions(conn)
        return [
            {"instrument": instrument, "units": pos["units"], "avg_price": pos["avg_price"]}
            for instrument, pos in open_positions.items()
        ]

    async def transactions(self, since_id: str | None = None) -> list[dict[str, Any]]:
        return []  # paper broker does not maintain a transaction ledger in v1

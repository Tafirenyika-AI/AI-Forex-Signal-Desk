"""Phase 0 sanity check: confirm the OANDA demo token works and print account state.

Run from the project root with the venv active:
    python -m src.scripts.check_connection
"""
from __future__ import annotations

import asyncio

from src.broker.oanda import OandaBroker
from src.config import load_settings


async def main() -> None:
    settings = load_settings()
    print(f"Environment: {settings.oanda_environment}")
    print(f"REST host:   {settings.oanda_rest_host}")

    async with OandaBroker(settings) as broker:
        accounts = await broker.list_accounts()
        print(f"Accounts visible to this token: {accounts}")

        state = await broker.account_state()
        print("\nAccount summary:")
        print(f"  account_id          = {state.account_id}")
        print(f"  currency            = {state.currency}")
        print(f"  balance             = {state.balance}")
        print(f"  NAV                 = {state.nav}")
        print(f"  unrealized_pl       = {state.unrealized_pl}")
        print(f"  margin_used         = {state.margin_used}")
        print(f"  margin_available    = {state.margin_available}")
        print(f"  open_trade_count    = {state.open_trade_count}")
        print(f"  open_position_count = {state.open_position_count}")

        prices = await broker.get_current_prices(["EUR_USD", "GBP_USD", "USD_JPY"])
        print("\nCurrent prices:")
        for p in prices:
            print(f"  {p.instrument}: bid={p.bid} ask={p.ask} spread={p.spread:.5f} time={p.time}")

    if not settings.oanda_account_id:
        print(
            f"\nTip: set OANDA_ACCOUNT_ID={state.account_id} in your .env "
            "to avoid re-resolving it on every run."
        )


if __name__ == "__main__":
    asyncio.run(main())

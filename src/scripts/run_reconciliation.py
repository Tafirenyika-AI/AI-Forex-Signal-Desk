"""Equity V2 Phase 1 scheduled entrypoint — runs src/reconciliation/
alpaca.py's reconcile() for every user with Alpaca configured, every
cycle. Read-only against the broker (see that module's own docstring for
why this is true by construction, not just by convention) — this script
only ever reads and persists findings, it never places, modifies, or
cancels an order.

Run periodically (every 30-60 minutes is plenty — broker state doesn't
change meaningfully faster than that, and this is a safety/audit check,
not a trading decision with a tight latency requirement).

Run from the project root with the venv active:
    python -m src.scripts.run_reconciliation
"""
from __future__ import annotations

import asyncio

from src.auth.service import active_trading_users
from src.broker.alpaca import AlpacaBroker
from src.config import load_settings
from src.data.db import get_engine
from src.reconciliation.alpaca import reconcile


async def main() -> None:
    settings = load_settings()
    engine = get_engine(settings.db_path)

    for user_ctx in active_trading_users(engine):
        if not user_ctx.settings.alpaca_api_key:
            continue
        async with AlpacaBroker(user_ctx.settings) as broker:
            report = await reconcile(engine, broker, user_ctx.user_id)

        by_severity: dict[str, int] = {}
        for issue in report.issues:
            by_severity[issue.severity] = by_severity.get(issue.severity, 0) + 1
        summary = ", ".join(f"{n} {sev}" for sev, n in sorted(by_severity.items()))
        print(f"{user_ctx.email}: reconciliation complete ({summary}) — worst: {report.worst_severity}")
        print(f"  Broker-verified P&L: ${report.broker_verified_pl:+,.2f}  |  "
              f"Internal-calculated P&L: ${report.internal_calculated_pl:+,.2f}")

        for issue in report.issues:
            if issue.severity == "CRITICAL":
                print(f"  CRITICAL [{issue.symbol}/{issue.issue_type}]: {issue.description}")


if __name__ == "__main__":
    asyncio.run(main())

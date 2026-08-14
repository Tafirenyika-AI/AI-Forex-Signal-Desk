"""Records a snapshot of the current promotion-gate report (Autonomous
Upgrade Spec sec. 18) — see src/evaluation/promotion_gates.py for what each
gate actually measures and why nothing here is ever auto-promoted.

Weekly cadence matches spec sec. 20's own "Weekly: ... challenger
comparison, strategy degradation check" bucket — promotion readiness is
exactly that kind of slow-moving check, not something worth computing more
often than the evidence underneath it actually changes.

Run from the project root with the venv active:
    python -m src.scripts.snapshot_promotion_gates
"""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone

from sqlalchemy import insert

from src.auth.service import active_trading_users
from src.config import load_settings
from src.data.db import get_engine
from src.data.db import promotion_gate_snapshots as promotion_gate_snapshots_table
from src.evaluation.promotion_gates import build_report


def main() -> None:
    settings = load_settings()
    engine = get_engine(settings.db_path)
    now = datetime.now(timezone.utc)

    for user_ctx in active_trading_users(engine):
        report = build_report(engine, user_ctx.user_id, now)
        with engine.begin() as conn:
            conn.execute(
                insert(promotion_gate_snapshots_table).values(
                    user_id=user_ctx.user_id,
                    current_phase=report.current_phase,
                    target_phase=report.target_phase,
                    ready_for_promotion=report.ready_for_promotion,
                    criteria_json=json.dumps([dataclasses.asdict(c) for c in report.criteria]),
                    computed_at=now,
                )
            )

        print(f"\n{user_ctx.email}: Phase {report.current_phase} -> {report.target_phase}: ready={report.ready_for_promotion}")
        for c in report.criteria:
            status = "PASS" if c.passed else ("FAIL" if c.passed is False else "insufficient data")
            print(f"  [{status}] {c.name}: {c.current_value}")


if __name__ == "__main__":
    main()

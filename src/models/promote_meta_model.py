"""Manual override for meta-model deployment. src.models.train_meta_model
auto-deploys a fresh candidate on its own when it clears a basic accuracy
floor (user-requested 2026-09-16) — this script is for the two cases that
doesn't cover: a candidate that missed the floor but you want live anyway
after reviewing it by hand, or rolling back to an older version.

    python -m src.models.promote_meta_model --list
    python -m src.models.promote_meta_model <version>
"""
from __future__ import annotations

import argparse
import json

from sqlalchemy import select, update

from src.config import load_settings
from src.data.db import get_engine
from src.data.db import model_registry as model_registry_table
from src.models.train_meta_model import MODEL_NAME


def list_versions(engine) -> None:
    with engine.connect() as conn:
        rows = conn.execute(
            select(model_registry_table)
            .where(model_registry_table.c.name == MODEL_NAME)
            .order_by(model_registry_table.c.trained_at.desc())
        ).mappings().all()
    if not rows:
        print("No meta-model versions trained yet — run src.models.train_meta_model first.")
        return
    for row in rows:
        validation = json.loads(row["validation_json"] or "{}")
        deployed_flag = " [DEPLOYED]" if row["deployed"] else ""
        print(f"{row['version']}{deployed_flag}")
        print(f"  trained_at={row['trained_at']}  n_samples={validation.get('n_samples')}  "
              f"cv_accuracy={validation.get('cv_accuracy_mean', 0):.1%} "
              f"(+/- {validation.get('cv_accuracy_std', 0):.1%})  "
              f"class_balance={validation.get('class_balance')}  "
              # baseline_accuracy: added 2026-09-23 (external review, P1-02)
              # -- "always guess the majority class" scores this for free,
              # with zero real skill; a human promoting by hand should see
              # it right next to cv_accuracy, not have to compute it.
              f"majority_class_baseline={validation.get('baseline_accuracy', 0):.1%}")


def promote(engine, version: str) -> None:
    with engine.connect() as conn:
        target = conn.execute(
            select(model_registry_table)
            .where(model_registry_table.c.name == MODEL_NAME)
            .where(model_registry_table.c.version == version)
        ).mappings().first()
    if target is None:
        print(f"No meta-model version '{version}' found. Run --list to see available versions.")
        return

    with engine.begin() as conn:
        conn.execute(
            update(model_registry_table)
            .where(model_registry_table.c.name == MODEL_NAME)
            .values(deployed=False)
        )
        conn.execute(
            update(model_registry_table)
            .where(model_registry_table.c.name == MODEL_NAME)
            .where(model_registry_table.c.version == version)
            .values(deployed=True)
        )
    print(f"Promoted meta-model v{version} to deployed. "
          f"run_loop.py will pick it up on its next start.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Promote/inspect trained meta-model versions")
    parser.add_argument("version", nargs="?", help="Version string to promote (see --list)")
    parser.add_argument("--list", action="store_true", help="List trained versions and their validation stats")
    args = parser.parse_args()

    settings = load_settings()
    engine = get_engine(settings.db_path)

    if args.list or not args.version:
        list_versions(engine)
    else:
        promote(engine, args.version)


if __name__ == "__main__":
    main()

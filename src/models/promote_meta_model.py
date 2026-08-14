"""Deploying a trained meta-model is a deliberate human decision, not an
automatic step after training — this script is that decision point.

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
              f"class_balance={validation.get('class_balance')}")


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

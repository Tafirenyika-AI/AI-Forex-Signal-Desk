"""Trains the real meta-model the blueprint specifies (sec. 6): a
calibration layer fit on component outputs vs. realized outcomes, replacing
decision/fusion.py's fixed-weight heuristic blend once there's enough
history to trust it.

Run from the project root with the venv active:
    python -m src.models.train_meta_model

Refuses to train below MIN_SAMPLES (overfitting risk on a handful of
trades is worse than staying with the honest heuristic) and reports
cross-validated accuracy, not training accuracy — with this few samples,
training accuracy is close to meaningless.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import cross_val_score
from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.data.db import model_registry as model_registry_table
from src.data.db import predictions as predictions_table
from src.data.db import upsert_insert as insert
from src.data.db import trade_outcomes as trade_outcomes_table

MIN_SAMPLES = 30
COMPONENTS = ["price", "macro", "cross_market", "news"]
MODEL_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "models"
MODEL_NAME = "meta_model"


def _load_linked_features(engine: Engine) -> pd.DataFrame:
    """Rebuilds the (instrument, horizon, time) key for each outcome's
    trade_intent, pulls that cycle's four component predictions, and
    expresses each component's score as *agreement with the direction
    actually traded* (score * +1 for BUY, * -1 for SELL) rather than the
    raw signed score. This matters: a negative price_score is bearish
    disagreement on a BUY but bearish *agreement* on a SELL — feeding the
    meta-model raw signed scores would have it learning direction-dependent
    noise instead of "how much did this component actually support the
    trade that was taken," which is the question it's supposed to answer."""
    from src.data.db import trade_intents as trade_intents_table

    with engine.connect() as conn:
        joined = conn.execute(
            select(
                trade_outcomes_table.c.id.label("outcome_id"),
                trade_outcomes_table.c.outcome,
                trade_intents_table.c.instrument,
                trade_intents_table.c.horizon,
                trade_intents_table.c.time,
                trade_intents_table.c.action,
            )
            .select_from(trade_outcomes_table)
            .join(trade_intents_table, trade_intents_table.c.id == trade_outcomes_table.c.trade_intent_id)
            .where(trade_outcomes_table.c.outcome.in_(["WIN", "LOSS"]))
            # OANDA only: macro/news/cross_market/session components are all
            # trivially (0.0, 0.0) for non-forex instruments (see Phase 1's
            # guards in src/risk/governor.py and the src/models/*_model.py
            # score functions) — mixing in Alpaca outcomes now that they're
            # tracked (src/outcomes/alpaca_tracker.py) would train this
            # shared meta-model on two structurally different feature
            # distributions without it knowing that's what's happening.
            # Revisit once Alpaca has its own real signal richness or
            # enough volume to justify a separate model.
            .where(trade_outcomes_table.c.broker == "oanda")
        ).mappings().all()

        records = []
        for row in joined:
            preds = conn.execute(
                select(predictions_table)
                .where(predictions_table.c.instrument == row["instrument"])
                .where(predictions_table.c.horizon == row["horizon"])
                .where(predictions_table.c.time == row["time"])
            ).mappings().all()
            by_component = {p["component"]: p for p in preds}
            if not all(c in by_component for c in COMPONENTS):
                continue  # incomplete component set for this cycle — skip rather than impute

            direction = 1 if row["action"] == "BUY" else -1
            record = {"label": 1 if row["outcome"] == "WIN" else 0}
            for c in COMPONENTS:
                raw = json.loads(by_component[c]["raw_json"] or "{}")
                record[f"{c}_agreement"] = raw.get("score", 0.0) * direction
                record[f"{c}_conf"] = by_component[c]["confidence"] or 0.0
            records.append(record)

    return pd.DataFrame(records)


def train(engine: Engine) -> dict | None:
    df = _load_linked_features(engine)
    if len(df) < MIN_SAMPLES:
        print(f"Only {len(df)} linked WIN/LOSS outcomes available (need {MIN_SAMPLES}+). "
              f"Staying with the fixed-weight heuristic in decision/fusion.py until more accumulate.")
        return None

    feature_cols = [f"{c}_{suffix}" for c in COMPONENTS for suffix in ("agreement", "conf")]
    X = df[feature_cols].to_numpy()
    y = df["label"].to_numpy()

    if len(np.unique(y)) < 2:
        print("All outcomes so far are the same class (all wins or all losses) — "
              "can't fit or validate a classifier on that. Waiting for more variety.")
        return None

    cv_folds = min(5, min(np.bincount(y)))  # can't have more folds than the smaller class
    if cv_folds < 2:
        print("Too few examples of the minority class for cross-validation yet.")
        return None

    model = LogisticRegression(max_iter=1000)
    cv_scores = cross_val_score(model, X, y, cv=cv_folds, scoring="accuracy")
    model.fit(X, y)  # final model trained on everything, for deployment

    now = datetime.now(timezone.utc)
    version = now.strftime("%Y%m%d_%H%M%S")
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    model_path = MODEL_DIR / f"{MODEL_NAME}_{version}.joblib"
    joblib.dump({"model": model, "feature_cols": feature_cols}, model_path)

    validation = {
        "n_samples": len(df),
        "cv_folds": cv_folds,
        "cv_accuracy_mean": float(cv_scores.mean()),
        "cv_accuracy_std": float(cv_scores.std()),
        "class_balance": {"win": int(y.sum()), "loss": int(len(y) - y.sum())},
    }

    with engine.begin() as conn:
        conn.execute(
            insert(model_registry_table).values(
                name=MODEL_NAME,
                version=version,
                trained_at=now,
                train_start=None,
                train_end=None,
                validation_json=json.dumps(validation),
                deployed=False,  # promoted separately, deliberately — see module docstring
            )
        )

    print(f"Trained meta-model v{version} on {len(df)} samples.")
    print(f"  Cross-validated accuracy: {cv_scores.mean():.1%} (+/- {cv_scores.std():.1%}, {cv_folds}-fold)")
    print(f"  Class balance: {validation['class_balance']}")
    print(f"  Saved to {model_path}")
    print(f"  NOT auto-deployed — inspect validation, then promote via "
          f"src.models.promote_meta_model if it looks trustworthy.")
    return validation


def load_deployed_meta_model(engine: Engine):
    """Returns (model, feature_cols) for the currently deployed meta-model,
    or None if none has been promoted yet — decision/fusion.py's fixed-
    weight heuristic is the correct fallback in that case, not an error."""
    with engine.connect() as conn:
        row = conn.execute(
            select(model_registry_table)
            .where(model_registry_table.c.name == MODEL_NAME)
            .where(model_registry_table.c.deployed.is_(True))
            .order_by(model_registry_table.c.trained_at.desc())
        ).mappings().first()
    if row is None:
        return None

    model_path = MODEL_DIR / f"{MODEL_NAME}_{row['version']}.joblib"
    if not model_path.exists():
        return None  # registry says deployed, but the artifact is gone — fail safe to heuristic
    bundle = joblib.load(model_path)
    return bundle["model"], bundle["feature_cols"]


if __name__ == "__main__":
    from src.config import load_settings
    from src.data.db import get_engine

    settings = load_settings()
    engine = get_engine(settings.db_path)
    train(engine)

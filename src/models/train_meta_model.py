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

Auto-deploys on success (user-requested 2026-09-16 — previously this
required a manual review-then-promote step every time; see
AUTO_DEPLOY_MIN_ACCURACY's own comment for the one sanity floor kept).
Run this nightly (already scheduled) to let the model retrain and
redeploy itself as more real outcomes accumulate. src.models.
promote_meta_model still exists for the case a candidate misses the
auto-deploy floor but you want it live anyway, or to roll back to an
older version.
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
from sqlalchemy import select, update
from sqlalchemy.engine import Engine

from src.data.db import model_registry as model_registry_table
from src.data.db import predictions as predictions_table
from src.data.db import upsert_insert as insert
from src.data.db import trade_outcomes as trade_outcomes_table

MIN_SAMPLES = 30
COMPONENTS = ["price", "macro", "cross_market", "news"]
MODEL_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "models"
MODEL_NAME = "meta_model"

# User-requested 2026-09-16: auto-deploy a freshly trained candidate
# instead of requiring a manual src.models.promote_meta_model review every
# time. One sanity floor kept even so — a candidate that trains
# successfully but scores at or below chance (0.5) would actively make
# live decisions worse than the fixed heuristic it's replacing, not just
# "not better yet." Below this floor it's still saved to model_registry
# (deployed=False) and reviewable/promotable by hand, same as before;
# only the "looks fine, ship it" case skips the manual step now.
AUTO_DEPLOY_MIN_ACCURACY = 0.5

# Real gap found 2026-09-23 (external review, P1-02): a flat 50% floor is
# the wrong baseline whenever the two classes aren't roughly balanced — a
# classifier that always predicts the MAJORITY class clears 50% for free
# on any imbalanced sample, with zero actual skill. Confirmed live: the
# model deployed on 2026-09-23 (36 samples, 10 win / 26 loss) scored
# 72.14% cv-accuracy — but always guessing "loss" on that same data scores
# 72.22%, i.e. this model's reported accuracy was statistically
# INDISTINGUISHABLE from having learned nothing at all, despite clearing
# the flat 50% floor by 22 points. User-approved fix (2026-09-23, after
# being shown this exact live example): auto-deploy now requires beating
# the MAJORITY-CLASS baseline (not just 50%) by a real margin — enough to
# rule out "got lucky within the trivial baseline's own noise," not just
# "numerically higher than it." AUTO_DEPLOY_MIN_ACCURACY (0.5) is kept as
# an absolute floor underneath this — a model could theoretically beat an
# EXTREME majority-class baseline (e.g. 95% one-sided data) while still
# being worse than a coin flip in absolute terms, which should never
# auto-deploy either.
AUTO_DEPLOY_MIN_MARGIN_OVER_BASELINE = 0.05  # 5 percentage points


def required_accuracy_for_auto_deploy(baseline_accuracy: float) -> float:
    """The bar a candidate's cv accuracy must clear to auto-deploy — pure
    function, separately testable from train()'s file/DB I/O. See
    AUTO_DEPLOY_MIN_MARGIN_OVER_BASELINE's own comment for the rationale."""
    return max(AUTO_DEPLOY_MIN_ACCURACY, baseline_accuracy + AUTO_DEPLOY_MIN_MARGIN_OVER_BASELINE)


def load_linked_features(engine: Engine) -> pd.DataFrame:
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
    df = load_linked_features(engine)
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

    win_count = int(y.sum())
    loss_count = int(len(y) - y.sum())
    baseline_accuracy = max(win_count, loss_count) / len(y)  # always-predict-majority-class

    validation = {
        "n_samples": len(df),
        "cv_folds": cv_folds,
        "cv_accuracy_mean": float(cv_scores.mean()),
        "cv_accuracy_std": float(cv_scores.std()),
        "class_balance": {"win": win_count, "loss": loss_count},
        "baseline_accuracy": baseline_accuracy,
    }

    required_accuracy = required_accuracy_for_auto_deploy(baseline_accuracy)
    auto_deploy = cv_scores.mean() > required_accuracy
    with engine.begin() as conn:
        conn.execute(
            insert(model_registry_table).values(
                name=MODEL_NAME,
                version=version,
                trained_at=now,
                train_start=None,
                train_end=None,
                validation_json=json.dumps(validation),
                deployed=auto_deploy,
            )
        )
        if auto_deploy:
            # Demote any previously deployed version — deployed is a
            # single-current-version flag, not a set (same invariant
            # promote_meta_model.deploy() enforces for the manual path).
            conn.execute(
                update(model_registry_table)
                .where(model_registry_table.c.name == MODEL_NAME)
                .where(model_registry_table.c.version != version)
                .values(deployed=False)
            )

    print(f"Trained meta-model v{version} on {len(df)} samples.")
    print(f"  Cross-validated accuracy: {cv_scores.mean():.1%} (+/- {cv_scores.std():.1%}, {cv_folds}-fold)")
    print(f"  Class balance: {validation['class_balance']} (majority-class baseline: {baseline_accuracy:.1%})")
    print(f"  Saved to {model_path}")
    if auto_deploy:
        print(f"  AUTO-DEPLOYED (cv accuracy {cv_scores.mean():.1%} > required {required_accuracy:.1%} "
              f"[max of {AUTO_DEPLOY_MIN_ACCURACY:.0%} floor and baseline {baseline_accuracy:.1%} "
              f"+ {AUTO_DEPLOY_MIN_MARGIN_OVER_BASELINE:.0%} margin]) — run_loop.py will pick "
              f"this up on its next cycle.")
    else:
        print(f"  NOT auto-deployed: cv accuracy {cv_scores.mean():.1%} does not clear the required "
              f"{required_accuracy:.1%} [max of {AUTO_DEPLOY_MIN_ACCURACY:.0%} floor and baseline "
              f"{baseline_accuracy:.1%} + {AUTO_DEPLOY_MIN_MARGIN_OVER_BASELINE:.0%} margin] — "
              f"not meaningfully better than always guessing the majority class. Still saved "
              f"and reviewable — promote by hand via src.models.promote_meta_model if you "
              f"want it live anyway.")
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

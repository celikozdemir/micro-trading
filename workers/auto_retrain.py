"""
Auto-retrainer — periodically retrains ML models from accumulated entry_signals.

Reads labelled entry signals from the DB, trains XGBoost models for
long/short directions, and saves them. The live MLScorer hot-reloads
the new models without requiring a restart.

Can run as a cron job or one-shot:
    python -m workers.auto_retrain                 # one-shot
    python -m workers.auto_retrain --loop 3600     # retrain every hour

Requires at least --min-samples labelled entries to train (default 100).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone

import numpy as np
import xgboost as xgb
from sklearn.metrics import precision_score, recall_score, roc_auc_score
from sqlalchemy import text

from backend.core.ml.features import FEATURE_NAMES, NUM_FEATURES
from backend.db.session import AsyncSessionLocal, engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

MIN_SAMPLES_DEFAULT = 100
THRESHOLD_DEFAULT = 0.55
MODEL_DIR_DEFAULT = "models"


async def load_labelled_signals() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Load all entry signals with outcomes from DB.
    
    Returns (features, label_long, label_short) arrays.
    """
    feature_cols = ", ".join(FEATURE_NAMES)
    query = f"""
        SELECT {feature_cols}, side, profitable, gross_pnl_bps
        FROM entry_signals
        WHERE profitable IS NOT NULL
        ORDER BY entry_time_ms ASC
    """

    rows = []
    async with AsyncSessionLocal() as session:
        result = await session.execute(text(query))
        rows = result.fetchall()

    if not rows:
        return np.array([]), np.array([]), np.array([])

    features = np.array([[row[i] for i in range(NUM_FEATURES)] for row in rows], dtype=np.float32)
    
    label_long = np.array([
        1.0 if (row[NUM_FEATURES] == "BUY" and row[NUM_FEATURES + 1]) else 0.0
        for row in rows
    ], dtype=np.float32)
    
    label_short = np.array([
        1.0 if (row[NUM_FEATURES] == "SELL" and row[NUM_FEATURES + 1]) else 0.0
        for row in rows
    ], dtype=np.float32)

    return features, label_long, label_short


def train_model(
    X: np.ndarray, y: np.ndarray, direction: str, threshold: float,
) -> tuple[xgb.XGBClassifier | None, dict]:
    """Train a single direction model. Returns (model, metrics) or (None, {}) if insufficient data."""
    
    pos = y.sum()
    neg = len(y) - pos
    if pos < 5 or neg < 5:
        log.warning("Skipping %s model — insufficient class balance (pos=%d, neg=%d)", direction, int(pos), int(neg))
        return None, {}

    split = int(len(X) * 0.8)
    X_tr, X_te = X[:split], X[split:]
    y_tr, y_te = y[:split], y[split:]

    scale_pos = (len(y_tr) - y_tr.sum()) / y_tr.sum() if y_tr.sum() > 0 else 1.0

    model = xgb.XGBClassifier(
        n_estimators=200,
        max_depth=4,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        scale_pos_weight=scale_pos,
        eval_metric="logloss",
        early_stopping_rounds=20,
        tree_method="hist",
        random_state=42,
    )
    model.fit(X_tr, y_tr, eval_set=[(X_te, y_te)], verbose=False)

    y_prob = model.predict_proba(X_te)[:, 1]
    y_pred = (y_prob >= threshold).astype(int)

    auc = roc_auc_score(y_te, y_prob) if len(np.unique(y_te)) > 1 else 0.5
    prec = precision_score(y_te, y_pred, zero_division=0)
    rec = recall_score(y_te, y_pred, zero_division=0)

    importance = dict(zip(FEATURE_NAMES, model.feature_importances_.tolist()))
    sorted_imp = dict(sorted(importance.items(), key=lambda x: x[1], reverse=True))

    metrics = {
        "direction": direction,
        "auc_roc": round(auc, 4),
        "precision": round(prec, 4),
        "recall": round(rec, 4),
        "train_samples": len(y_tr),
        "test_samples": len(y_te),
        "positive_rate": round(float(y.mean()), 4),
        "feature_importance": sorted_imp,
    }

    log.info(
        "%s model: AUC=%.4f Prec=%.4f Rec=%.4f (train=%d, test=%d)",
        direction.upper(), auc, prec, rec, len(y_tr), len(y_te),
    )

    return model, metrics


async def retrain_once(min_samples: int, threshold: float, model_dir: str) -> bool:
    """Run one retraining cycle. Returns True if models were updated."""
    
    log.info("Loading labelled entry signals from DB...")
    features, label_long, label_short = await load_labelled_signals()

    n = len(features)
    log.info("Found %d labelled signals", n)

    if n < min_samples:
        log.info("Need at least %d samples, have %d — skipping retrain", min_samples, n)
        return False

    os.makedirs(model_dir, exist_ok=True)
    all_metrics = {}
    models_saved = 0

    for direction, labels in [("long", label_long), ("short", label_short)]:
        model, metrics = train_model(features, labels, direction, threshold)
        if model is not None:
            model_path = os.path.join(model_dir, f"signal_{direction}.json")
            model.save_model(model_path)
            all_metrics[direction] = metrics
            models_saved += 1
            log.info("Saved %s model → %s", direction, model_path)

    if models_saved == 0:
        log.warning("No models trained — insufficient data quality")
        return False

    meta = {
        "threshold": threshold,
        "feature_names": FEATURE_NAMES,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "total_samples": n,
        "models": all_metrics,
    }
    meta_path = os.path.join(model_dir, "model_meta.json")
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)
    log.info("Saved metadata → %s", meta_path)

    # Signal the live scorer to reload by touching a marker file
    marker = os.path.join(model_dir, ".reload")
    with open(marker, "w") as f:
        f.write(datetime.now(timezone.utc).isoformat())

    log.info("Retrain complete — %d models updated", models_saved)
    return True


async def main():
    parser = argparse.ArgumentParser(description="Auto-retrain ML models from live entry signals")
    parser.add_argument("--min-samples", type=int, default=MIN_SAMPLES_DEFAULT)
    parser.add_argument("--threshold", type=float, default=THRESHOLD_DEFAULT)
    parser.add_argument("--model-dir", default=MODEL_DIR_DEFAULT)
    parser.add_argument("--loop", type=int, default=0, help="Retrain interval in seconds (0 = one-shot)")
    args = parser.parse_args()

    if args.loop > 0:
        log.info("Auto-retrain loop: every %ds, min_samples=%d", args.loop, args.min_samples)
        while True:
            try:
                await retrain_once(args.min_samples, args.threshold, args.model_dir)
            except Exception as e:
                log.error("Retrain failed: %s", e)
            await asyncio.sleep(args.loop)
    else:
        await retrain_once(args.min_samples, args.threshold, args.model_dir)


if __name__ == "__main__":
    asyncio.run(main())

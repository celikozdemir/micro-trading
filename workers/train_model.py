"""
Train XGBoost classifier for ML signal scoring.

Reads the parquet training data produced by build_training_data.py,
trains separate models for LONG and SHORT entry signals, and saves
them along with evaluation metrics.

Usage:
    python -m workers.train_model --data data/training_BTCUSDT_5d.parquet
    python -m workers.train_model --data data/training_BTCUSDT_5d.parquet --threshold 0.60
"""

from __future__ import annotations

import argparse
import json
import os
import time

import joblib
import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.metrics import (
    accuracy_score,
    classification_report,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.model_selection import TimeSeriesSplit

from backend.core.ml.features import FEATURE_NAMES


def train_direction_model(
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_test: np.ndarray,
    y_test: np.ndarray,
    direction: str,
    threshold: float = 0.55,
) -> tuple[xgb.XGBClassifier, dict]:
    """Train and evaluate a single direction model (long or short)."""

    pos_count = y_train.sum()
    neg_count = len(y_train) - pos_count
    scale_pos = neg_count / pos_count if pos_count > 0 else 1.0

    model = xgb.XGBClassifier(
        n_estimators=300,
        max_depth=5,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        scale_pos_weight=scale_pos,
        eval_metric="logloss",
        early_stopping_rounds=30,
        tree_method="hist",
        random_state=42,
    )

    model.fit(
        X_train, y_train,
        eval_set=[(X_test, y_test)],
        verbose=False,
    )

    # Evaluate
    y_prob = model.predict_proba(X_test)[:, 1]
    y_pred = (y_prob >= threshold).astype(int)

    auc = roc_auc_score(y_test, y_prob) if len(np.unique(y_test)) > 1 else 0.0
    acc = accuracy_score(y_test, y_pred)
    prec = precision_score(y_test, y_pred, zero_division=0)
    rec = recall_score(y_test, y_pred, zero_division=0)

    # Feature importance
    importance = dict(zip(FEATURE_NAMES, model.feature_importances_.tolist()))
    sorted_imp = dict(sorted(importance.items(), key=lambda x: x[1], reverse=True))

    metrics = {
        "direction": direction,
        "threshold": threshold,
        "auc_roc": round(auc, 4),
        "accuracy": round(acc, 4),
        "precision": round(prec, 4),
        "recall": round(rec, 4),
        "train_samples": len(y_train),
        "test_samples": len(y_test),
        "train_positive_rate": round(float(y_train.mean()), 4),
        "test_positive_rate": round(float(y_test.mean()), 4),
        "predictions_at_threshold": int(y_pred.sum()),
        "feature_importance": sorted_imp,
        "n_estimators_used": model.best_iteration + 1 if hasattr(model, 'best_iteration') and model.best_iteration is not None else model.n_estimators,
    }

    print(f"\n{'='*60}")
    print(f"  {direction.upper()} Model Results")
    print(f"{'='*60}")
    print(f"  AUC-ROC:    {auc:.4f}")
    print(f"  Accuracy:   {acc:.4f}")
    print(f"  Precision:  {prec:.4f}  (at threshold={threshold})")
    print(f"  Recall:     {rec:.4f}")
    print(f"  Predictions:{y_pred.sum()} / {len(y_test)} test samples")
    print(f"  Best iter:  {metrics['n_estimators_used']}")
    print(f"\n  Top features:")
    for name, imp in list(sorted_imp.items())[:5]:
        print(f"    {name:25s} {imp:.4f}")

    return model, metrics


def walk_forward_eval(
    df: pd.DataFrame,
    direction: str,
    threshold: float,
    n_splits: int = 3,
) -> dict:
    """Time-series cross-validation to check for overfitting."""
    X = df[FEATURE_NAMES].values
    y = df[f"label_{direction}"].values

    tscv = TimeSeriesSplit(n_splits=n_splits)
    fold_aucs = []

    for fold, (train_idx, test_idx) in enumerate(tscv.split(X)):
        X_tr, X_te = X[train_idx], X[test_idx]
        y_tr, y_te = y[train_idx], y[test_idx]

        if len(np.unique(y_te)) < 2:
            continue

        pos = y_tr.sum()
        neg = len(y_tr) - pos
        spw = neg / pos if pos > 0 else 1.0

        m = xgb.XGBClassifier(
            n_estimators=200, max_depth=5, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8,
            scale_pos_weight=spw, eval_metric="logloss",
            early_stopping_rounds=20, tree_method="hist", random_state=42,
        )
        m.fit(X_tr, y_tr, eval_set=[(X_te, y_te)], verbose=False)
        y_prob = m.predict_proba(X_te)[:, 1]
        auc = roc_auc_score(y_te, y_prob)
        fold_aucs.append(auc)
        print(f"  Fold {fold+1}: AUC={auc:.4f} (train={len(y_tr):,}, test={len(y_te):,})")

    return {
        "fold_aucs": fold_aucs,
        "mean_auc": round(float(np.mean(fold_aucs)), 4) if fold_aucs else 0.0,
        "std_auc": round(float(np.std(fold_aucs)), 4) if fold_aucs else 0.0,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, help="Path to parquet training data")
    parser.add_argument("--threshold", type=float, default=0.55, help="Probability threshold for entry")
    parser.add_argument("--output-dir", default="models", help="Output directory for model files")
    args = parser.parse_args()

    print(f"Loading {args.data}...")
    df = pd.read_parquet(args.data)
    print(f"  {len(df):,} samples, columns: {list(df.columns)}")

    # Time-based train/test split (last 20% is test — mimics live deployment)
    split_idx = int(len(df) * 0.8)
    df_train = df.iloc[:split_idx]
    df_test = df.iloc[split_idx:]

    X_train = df_train[FEATURE_NAMES].values
    X_test = df_test[FEATURE_NAMES].values

    print(f"\nTrain: {len(df_train):,} samples ({df_train['ts_ms'].min():.0f} → {df_train['ts_ms'].max():.0f})")
    print(f"Test:  {len(df_test):,} samples ({df_test['ts_ms'].min():.0f} → {df_test['ts_ms'].max():.0f})")

    os.makedirs(args.output_dir, exist_ok=True)

    all_metrics = {}

    for direction in ["long", "short"]:
        y_train = df_train[f"label_{direction}"].values
        y_test = df_test[f"label_{direction}"].values

        model, metrics = train_direction_model(
            X_train, y_train, X_test, y_test,
            direction=direction,
            threshold=args.threshold,
        )

        # Walk-forward validation
        print(f"\n  Walk-forward CV ({direction}):")
        cv_metrics = walk_forward_eval(df, direction, args.threshold)
        metrics["cv"] = cv_metrics
        print(f"  Mean AUC: {cv_metrics['mean_auc']:.4f} ± {cv_metrics['std_auc']:.4f}")

        # Save model
        model_path = os.path.join(args.output_dir, f"signal_{direction}.json")
        model.save_model(model_path)
        print(f"\n  Saved model: {model_path}")

        all_metrics[direction] = metrics

    # Save combined metrics
    meta = {
        "threshold": args.threshold,
        "feature_names": FEATURE_NAMES,
        "data_file": args.data,
        "train_samples": len(df_train),
        "test_samples": len(df_test),
        "models": all_metrics,
    }
    meta_path = os.path.join(args.output_dir, "model_meta.json")
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)
    print(f"\nSaved metadata: {meta_path}")

    # Summary
    print(f"\n{'='*60}")
    print(f"  TRAINING COMPLETE")
    print(f"{'='*60}")
    for d in ["long", "short"]:
        m = all_metrics[d]
        print(f"  {d.upper():6s}: AUC={m['auc_roc']:.4f}  Prec={m['precision']:.4f}  "
              f"Rec={m['recall']:.4f}  CV={m['cv']['mean_auc']:.4f}±{m['cv']['std_auc']:.4f}")


if __name__ == "__main__":
    main()

"""
ML Signal Scorer — loads trained XGBoost models and produces
trade probability scores for live inference.

Supports hot-reload: the auto_retrain worker writes a .reload marker
file after saving new models. The scorer checks for this periodically
and reloads without requiring a process restart.

Usage in strategy:
    scorer = MLScorer("models/")
    prob = scorer.score(features, direction="long")
    if prob > scorer.threshold:
        # enter trade
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import numpy as np
import xgboost as xgb

from backend.core.ml.features import FEATURE_NAMES, NUM_FEATURES

logger = logging.getLogger(__name__)

_RELOAD_CHECK_INTERVAL_S = 30


class MLScorer:
    """
    Loads pre-trained XGBoost models for long/short signal scoring.
    Thread-safe for inference (XGBoost predict is read-only).
    Periodically checks for a .reload marker to hot-swap models.
    """

    def __init__(self, model_dir: str = "models"):
        self.model_dir = Path(model_dir)
        self.threshold: float = 0.55
        self._models: dict[str, xgb.XGBClassifier] = {}
        self._loaded = False
        self._last_reload_check: float = 0.0
        self._reload_marker_mtime: float = 0.0

        self._try_load()

    def _try_load(self) -> None:
        meta_path = self.model_dir / "model_meta.json"
        if not meta_path.exists():
            logger.warning("No ML models found at %s — scorer disabled", self.model_dir)
            return

        with open(meta_path) as f:
            meta = json.load(f)

        self.threshold = meta.get("threshold", 0.55)

        for direction in ["long", "short"]:
            model_path = self.model_dir / f"signal_{direction}.json"
            if model_path.exists():
                model = xgb.XGBClassifier()
                model.load_model(str(model_path))
                self._models[direction] = model
                logger.info(
                    "Loaded %s model (AUC=%.4f, threshold=%.2f)",
                    direction,
                    meta.get("models", {}).get(direction, {}).get("auc_roc", 0),
                    self.threshold,
                )

        self._loaded = len(self._models) > 0
        if self._loaded:
            logger.info("ML Scorer ready: %d models loaded", len(self._models))

        # Track reload marker
        marker = self.model_dir / ".reload"
        if marker.exists():
            self._reload_marker_mtime = marker.stat().st_mtime

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def _check_hot_reload(self) -> None:
        """Check if the auto-retrainer has produced new models."""
        now = time.monotonic()
        if now - self._last_reload_check < _RELOAD_CHECK_INTERVAL_S:
            return
        self._last_reload_check = now

        marker = self.model_dir / ".reload"
        if not marker.exists():
            return
        mtime = marker.stat().st_mtime
        if mtime > self._reload_marker_mtime:
            logger.info("New models detected — hot-reloading ML scorer")
            self._reload_marker_mtime = mtime
            self.reload()

    def score(self, features: np.ndarray, direction: str) -> float:
        """
        Return P(profitable) for the given direction.
        Returns 0.0 if no model is loaded for that direction.
        """
        self._check_hot_reload()

        model = self._models.get(direction)
        if model is None:
            return 0.0

        if features.ndim == 1:
            features = features.reshape(1, -1)

        prob = model.predict_proba(features)[0, 1]
        return float(prob)

    def should_enter(self, features: np.ndarray, direction: str) -> tuple[bool, float]:
        """
        Convenience: returns (should_trade, confidence).
        If scorer is not loaded, always returns (True, 1.0) to not block rule-based entries.
        """
        if not self._loaded:
            self._check_hot_reload()
        if not self._loaded:
            return True, 1.0

        prob = self.score(features, direction)
        return prob >= self.threshold, prob

    def reload(self) -> None:
        """Hot-reload models from disk (e.g. after retraining)."""
        self._models.clear()
        self._loaded = False
        self._try_load()

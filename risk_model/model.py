"""
Multi-Action Recovery Recommendation Model -- predicts P(recover | features, candidate_action).

Architecture decision: docs/adr/0004-uplift-model-design.md

Design: single XGBoost classifier with candidate_action as a feature.
At inference, the model is scored for all 4 candidate actions; the action
with the highest predicted P(recover) is returned as the recommendation.

Note on terminology: this is a multi-action recommendation model, not a causal
uplift/treatment-effect estimator. 'Uplift' in the causal inference sense estimates
the incremental treatment effect; this model selects the highest-P(recover) action
from the scored candidates. The distinction is documented in ADR 0004.

This is simpler and more sample-efficient than training 4 separate models,
and it allows the model to learn cross-action correlations (e.g., if retry_now
is bad, that evidence also informs the model about retry_delayed).
"""

from __future__ import annotations

import logging
import random
from pathlib import Path

import numpy as np
import xgboost as xgb

from ingestion.generator import generate_transactions
from risk_model.features import FeatureVector, extract_features
from risk_model.recovery_rates import get_contextual_recovery_rate
from risk_model.shap_explainer import SHAPExplainer
from schemas.decision import MODEL_CANDIDATE_ACTIONS, ModelDecision, RecoveryAction, SHAPFeature
from schemas.transaction import FailedTransaction

logger = logging.getLogger(__name__)

MODEL_PATH = Path("data/models/recovery_model.json")
TRAINING_TRANSACTIONS = 15_000  # transactions for model training (separate from test batch)
TRAINING_SEED = 99  # different seed from simulation to avoid data leakage


class RecoveryModel:
    """
    Multi-Action Recovery Recommendation Model (XGBoost-based).

    At inference, all 4 candidate actions are scored for a given transaction.
    The action with the highest P(recover) is recommended.

    SHAP values are computed per prediction for the winning action to produce
    the top-3 feature contributions fed to the LLM explanation layer.
    """
    def __init__(self) -> None:
        self._model: xgb.XGBClassifier | None = None
        self._shap_explainer: SHAPExplainer | None = None

    def _encode_row(self, features: FeatureVector, action: RecoveryAction) -> list[float]:
        """Encode a (features, action) pair as a single model input row."""
        # Action is encoded as 0–3 (same order as MODEL_CANDIDATE_ACTIONS)
        action_id = list(MODEL_CANDIDATE_ACTIONS).index(action)
        return features.to_list() + [float(action_id)]

    def train(self, n_training_txns: int = TRAINING_TRANSACTIONS, seed: int = TRAINING_SEED) -> None:
        """
        Generate labeled training data and fit the XGBoost classifier.

        Training data generation:
        - Generate n_training_txns synthetic transactions (separate seed from simulation)
        - For each transaction × each candidate action: sample a binary recovery outcome
          from the documented RECOVERY_RATES table (stochastic, so the model doesn't
          overfit to exact probabilities)
        - Stack into (n_txns × 4, n_features + 1) design matrix

        This constitutes supervised learning on synthetic labeled data.
        The synthetic nature is documented and honest — see docs/data_provenance.md.
        """
        logger.info("Generating %d training transactions (seed=%d)...", n_training_txns, seed)
        txns = generate_transactions(n=n_training_txns, random_seed=seed)
        rng = random.Random(seed)

        X_rows: list[list[float]] = []
        y_rows: list[int] = []

        for txn in txns:
            features = extract_features(txn)
            for action in MODEL_CANDIDATE_ACTIONS:
                row = self._encode_row(features, action)
                # Use contextual rate so labels encode amount/timing/history signal
                p_recover = get_contextual_recovery_rate(
                    failure_code=txn.failure_code,
                    action=action,
                    amount_inr=float(txn.amount_inr),
                    hour_of_day=txn.time_of_failure.hour,
                    prior_failed_attempts_30d=txn.retry_count_so_far,
                )
                # Stochastic label — not deterministic from p, so the model must
                # learn the underlying rate rather than memorising individual rows
                recovered = int(rng.random() < p_recover)
                X_rows.append(row)
                y_rows.append(recovered)

        X = np.array(X_rows, dtype=np.float32)
        y = np.array(y_rows, dtype=np.int32)

        logger.info("Training XGBoost on %d samples (%d features)...", len(X_rows), X.shape[1])

        self._model = xgb.XGBClassifier(
            n_estimators=150,
            max_depth=5,
            learning_rate=0.1,
            subsample=0.8,
            colsample_bytree=0.8,
            scale_pos_weight=float(np.sum(y == 0)) / max(float(np.sum(y == 1)), 1),
            random_state=seed,
            eval_metric="logloss",
            verbosity=0,
        )
        self._model.fit(X, y)
        self._shap_explainer = SHAPExplainer(self._model)
        logger.info("Model training complete.")

    def save(self, path: Path = MODEL_PATH) -> None:
        """Persist model to disk in native XGBoost JSON format. Creates parent directories if needed."""
        if self._model is None:
            raise RuntimeError("Cannot save uninitialized model.")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._model.save_model(str(path))
        logger.info("Model saved to %s (XGBoost JSON)", path)

    def load(self, path: Path = MODEL_PATH) -> None:
        """Load a previously saved XGBoost model from JSON disk artifact."""
        self._model = xgb.XGBClassifier()
        self._model.load_model(str(path))
        self._shap_explainer = SHAPExplainer(self._model)
        logger.info("Model loaded from %s", path)

    def load_or_train(self, path: Path = MODEL_PATH) -> None:
        """
        Load model from disk if available; otherwise train and save.
        This ensures the simulation is reproducible without re-training every run.
        """
        if path.exists():
            self.load(path)
        else:
            self.train()
            self.save(path)

    def predict(self, txn: FailedTransaction) -> ModelDecision:
        """
        Score all 4 candidate actions and return the best one as a ModelDecision.

        Returns the action with the highest predicted P(recover).
        Also computes SHAP values for the winning action (top-3 features).

        Raises RuntimeError if called before the model is loaded/trained.
        """
        if self._model is None:
            raise RuntimeError("Model not loaded. Call load_or_train() first.")

        features = extract_features(txn)
        rows = [self._encode_row(features, action) for action in MODEL_CANDIDATE_ACTIONS]
        X = np.array(rows, dtype=np.float32)

        proba = self._model.predict_proba(X)[:, 1]  # P(recover) for each action
        p_by_action = {
            action.value: float(p)
            for action, p in zip(MODEL_CANDIDATE_ACTIONS, proba, strict=False)
        }

        best_idx = int(np.argmax(proba))
        best_action = MODEL_CANDIDATE_ACTIONS[best_idx]
        best_confidence = float(proba[best_idx])

        # SHAP explanation for the winning action
        shap_features: list[SHAPFeature] = []
        if self._shap_explainer is not None:
            shap_features = self._shap_explainer.top_features(
                row=rows[best_idx],
                features=features,
                action=best_action,
                top_k=3,
            )

        delay: int | None = None
        if best_action == RecoveryAction.RETRY_DELAYED:
            delay = 60 * 24  # default: retry next day

        return ModelDecision(
            txn_id=txn.txn_id,
            recommended_action=best_action,
            confidence=best_confidence,
            retry_delay_minutes=delay,
            shap_top_features=shap_features if shap_features else [
                SHAPFeature(
                    feature_name="failure_code_category",
                    shap_value=0.0,
                    feature_value=txn.failure_code.value,
                    direction="positive",
                )
            ],
            p_recover_by_action=p_by_action,
        )

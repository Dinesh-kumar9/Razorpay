"""
SHAP global feature importance ablation.

Computes and prints the global SHAP feature importance across the trained XGBoost model.
Verifies that features beyond failure_code (amount_tier, hour_of_day, retry_attempt_number, etc.)
contribute meaningfully to recovery prediction.

Usage:
    python -m risk_model.shap_ablation
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np
import shap

from ingestion.generator import generate_transactions
from risk_model.features import FEATURE_NAMES, extract_features
from risk_model.model import MODEL_PATH, TRAINING_SEED, RecoveryModel
from schemas.decision import MODEL_CANDIDATE_ACTIONS

logger = logging.getLogger(__name__)


def run_shap_ablation(
    n_background: int = 500,
    seed: int = TRAINING_SEED,
    verbose: bool = True,
) -> dict[str, Any]:
    """
    Run SHAP global importance and return ranked feature importances.

    Args:
        n_background: number of transactions to use for SHAP background dataset
        seed: random seed for reproducibility
        verbose: whether to print table to stdout

    Returns:
        dict containing ranked feature importances, top_feature, top_pct, and non_failure_code_pct
    """
    model = RecoveryModel()
    model.load_or_train(Path(MODEL_PATH))

    txns = generate_transactions(n=n_background, random_seed=seed + 9999)

    # Build background dataset matching model input dimensions (9 features + 1 action_id)
    X_rows: list[list[float]] = []
    for txn in txns:
        features = extract_features(txn)
        for action in MODEL_CANDIDATE_ACTIONS:
            row = model._encode_row(features, action)
            X_rows.append(row)

    X = np.array(X_rows, dtype=np.float32)
    all_feature_names = FEATURE_NAMES + ["action_id"]

    explainer = shap.TreeExplainer(model._model)
    shap_values = explainer.shap_values(X)  # shape: (n_samples, n_features)

    # Global importance: mean absolute SHAP value per feature
    mean_abs_shap = np.abs(shap_values).mean(axis=0)
    ranked = sorted(zip(all_feature_names, mean_abs_shap), key=lambda x: x[1], reverse=True)

    total = sum(v for _, v in ranked)
    ranked_pct = [(name, val, (val / total * 100) if total > 0 else 0.0) for name, val in ranked]

    top_feature = ranked[0][0]
    top_pct = ranked_pct[0][2]
    non_failure_code_pct = sum(pct for name, _, pct in ranked_pct if "failure_code" not in name)

    if verbose:
        print("\n" + "=" * 65)
        print("SHAP GLOBAL FEATURE IMPORTANCE (mean |SHAP|)")
        print("=" * 65)
        for i, (name, importance, pct) in enumerate(ranked_pct, 1):
            bar = "█" * int(pct / 2)
            print(f"  {i:2d}. {name:<30} {importance:.4f}  ({pct:5.1f}%)  {bar}")

        print("\n" + "-" * 65)
        print(f"  Top Feature:               '{top_feature}' ({top_pct:.1f}%)")
        print(f"  Non-Failure-Code Signal:   {non_failure_code_pct:.1f}%")
        print("=" * 65)

    return {
        "ranked_features": ranked_pct,
        "top_feature": top_feature,
        "top_pct": top_pct,
        "non_failure_code_pct": non_failure_code_pct,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    run_shap_ablation(n_background=500, verbose=True)

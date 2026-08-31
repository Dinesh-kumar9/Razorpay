"""
SHAP global feature importance ablation.

Computes and prints the global SHAP feature importance across the trained XGBoost model.
Run this after training the model to verify that features beyond failure_code
(amount_tier, hour_of_day, retry_attempt_number, etc.) contribute meaningfully.

If failure_code_category dominates to the exclusion of all others, that would
indicate the context modifiers in recovery_rates.py are not magnitude-significant
enough to create learnable signal. Adjust _AMOUNT_HIGH_PENALTY, _BANK_HOURS_BONUS,
and _REPEAT_FAILURE_PENALTY in recovery_rates.py to increase signal strength.

Usage:
    python -m risk_model.shap_ablation

Output: ranked feature importances (mean |SHAP|) per feature name.
"""

from __future__ import annotations

import random
from pathlib import Path

import numpy as np

from ingestion.generator import generate_transactions
from risk_model.features import FEATURE_NAMES, extract_features
from risk_model.model import MODEL_PATH, TRAINING_SEED, RecoveryModel
from risk_model.recovery_rates import get_contextual_recovery_rate
from schemas.decision import MODEL_CANDIDATE_ACTIONS, RecoveryAction

# Action encoding offset (same as model._encode_row)
_ACTION_ENCODING = {
    RecoveryAction.RETRY_NOW: [1, 0, 0, 0],
    RecoveryAction.RETRY_DELAYED: [0, 1, 0, 0],
    RecoveryAction.NUDGE_ALT_METHOD: [0, 0, 1, 0],
    RecoveryAction.ESCALATE_TO_HUMAN: [0, 0, 0, 1],
}


def run_shap_ablation(n_background: int = 500, seed: int = TRAINING_SEED) -> None:
    """
    Run SHAP global importance and print ranked feature table.

    Args:
        n_background: number of transactions to use for SHAP background dataset
        seed: random seed for reproducibility
    """
    import shap

    print("Loading / training model...")
    model = RecoveryModel()
    model.load_or_train(Path(MODEL_PATH))

    print(f"Generating {n_background} background transactions (seed={seed})...")
    txns = generate_transactions(n=n_background, random_seed=seed + 9999)
    rng = random.Random(seed + 9999)

    # Build background dataset: one row per (txn, action) pair
    X_rows = []
    for txn in txns:
        features = extract_features(txn)
        for action in MODEL_CANDIDATE_ACTIONS:
            row = features.to_list() + _ACTION_ENCODING[action]
            X_rows.append(row)

    X = np.array(X_rows, dtype=np.float32)

    # Full feature names including action one-hot
    action_names = [f"action_{a.value}" for a in MODEL_CANDIDATE_ACTIONS]
    all_feature_names = FEATURE_NAMES + action_names

    print("Computing SHAP values (TreeExplainer)...")
    explainer = shap.TreeExplainer(model._model)
    shap_values = explainer.shap_values(X)  # shape: (n_samples, n_features)

    # Global importance: mean absolute SHAP value per feature
    mean_abs_shap = np.abs(shap_values).mean(axis=0)
    ranked = sorted(zip(all_feature_names, mean_abs_shap), key=lambda x: x[1], reverse=True)

    print("\n" + "=" * 60)
    print("SHAP GLOBAL FEATURE IMPORTANCE (mean |SHAP|)")
    print("=" * 60)
    total = sum(v for _, v in ranked)
    for i, (name, importance) in enumerate(ranked, 1):
        pct = importance / total * 100
        bar = "█" * int(pct / 2)
        print(f"  {i:2d}. {name:<35} {importance:.4f}  ({pct:5.1f}%)  {bar}")

    print("\nAblation check:")
    feature_vals = dict(ranked)
    top_feature = ranked[0][0]
    top_pct = ranked[0][1] / total * 100

    if top_feature == "failure_code_category" and top_pct > 70:
        print(f"  ⚠️  WARNING: failure_code_category dominates at {top_pct:.1f}%")
        print("     Context modifiers may not be magnitude-significant.")
        print("     Consider increasing _AMOUNT_HIGH_PENALTY or _BANK_HOURS_BONUS")
        print("     in risk_model/recovery_rates.py.")
    else:
        print(f"  ✅ Healthy signal distribution: top feature '{top_feature}' = {top_pct:.1f}%")
        non_code_pct = sum(v for name, v in ranked if "failure_code" not in name) / total * 100
        print(f"     Non-failure-code features contribute {non_code_pct:.1f}% of total SHAP signal.")

    print("=" * 60)


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)
    run_shap_ablation()

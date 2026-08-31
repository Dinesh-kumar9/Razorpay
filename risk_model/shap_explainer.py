"""
SHAP explainer — computes top-k feature contributions for the winning action.

Uses TreeExplainer (exact, not approximate) for XGBoost.
The top-3 features are passed to the LLM explanation layer as structured
context — they tell the LLM *why* the model made the recommendation it did,
which produces better rationales than passing raw SHAP values.
"""

from __future__ import annotations

import logging

import numpy as np
import shap  # type: ignore[import]
import xgboost as xgb  # type: ignore[import]

from risk_model.features import FEATURE_NAMES, FeatureVector
from schemas.decision import RecoveryAction, SHAPFeature

logger = logging.getLogger(__name__)


class SHAPExplainer:
    """
    Wraps shap.TreeExplainer for a trained XGBoost model.

    Instantiated once per model load and reused across all predictions in a batch,
    since TreeExplainer construction is expensive but explain() calls are cheap.
    """

    # Feature names in model order (features + action_id appended at the end)
    ALL_FEATURE_NAMES: list[str] = FEATURE_NAMES + ["candidate_action_id"]

    def __init__(self, model: xgb.XGBClassifier) -> None:
        self._explainer = shap.TreeExplainer(model)

    def top_features(
        self,
        row: list[float],
        features: FeatureVector,
        action: RecoveryAction,
        top_k: int = 3,
    ) -> list[SHAPFeature]:
        """
        Compute SHAP values for a single (features, action) row and return
        the top-k features by absolute SHAP value.

        The returned SHAPFeature list is used as context in the LLM prompt.
        We exclude the action_id feature from the top-k since it's not
        interpretable to a merchant ops analyst.
        """
        try:
            X = np.array([row], dtype=np.float32)
            shap_vals = self._explainer.shap_values(X)

            # shap_vals shape: (1, n_features) for binary classification
            if isinstance(shap_vals, list):
                # Some XGBoost versions return [neg_class_vals, pos_class_vals]
                vals = shap_vals[1][0]
            else:
                vals = shap_vals[0]

            # Exclude the last feature (candidate_action_id)
            feature_vals = vals[:-1]
            feature_names = self.ALL_FEATURE_NAMES[:-1]

            # Sort by absolute value descending
            indices = sorted(range(len(feature_vals)), key=lambda i: abs(feature_vals[i]), reverse=True)
            top_indices = indices[:top_k]

            result: list[SHAPFeature] = []
            feature_list = features.to_list()
            for idx in top_indices:
                sv = float(feature_vals[idx])
                fname = feature_names[idx]
                fval = f"{feature_list[idx]:.2f}" if isinstance(feature_list[idx], float) else str(feature_list[idx])
                result.append(
                    SHAPFeature(
                        feature_name=fname,
                        shap_value=sv,
                        feature_value=fval,
                        direction="positive" if sv >= 0 else "negative",
                    )
                )

            return result

        except Exception as exc:
            logger.warning("SHAP computation failed (%s); returning empty feature list.", exc)
            return []

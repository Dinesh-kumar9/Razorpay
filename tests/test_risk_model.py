"""
Unit tests for the risk model, feature extraction, recovery rates, and SHAP explainer.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import numpy as np

from ingestion.generator import generate_transactions
from risk_model.features import extract_features, FEATURE_NAMES, FeatureVector
from risk_model.model import RecoveryModel, MODEL_PATH
from risk_model.recovery_rates import get_contextual_recovery_rate, get_recovery_rate, RECOVERY_RATES
from schemas.decision import RecoveryAction, MODEL_CANDIDATE_ACTIONS
from schemas.transaction import FailureCode, FailedTransaction


class TestRecoveryRates:
    def test_base_lookup(self):
        rate = get_recovery_rate(FailureCode.INSUFFICIENT_FUNDS, RecoveryAction.RETRY_DELAYED)
        assert rate == 0.42

    def test_hard_stop_always_zero_for_retry(self):
        for code in [FailureCode.CARD_BLOCKED, FailureCode.FRAUD_FLAG, FailureCode.KYC_HOLD]:
            rate = get_contextual_recovery_rate(
                failure_code=code,
                action=RecoveryAction.RETRY_NOW,
                amount_inr=500.0,
                hour_of_day=14,
                prior_failed_attempts_30d=0,
            )
            assert rate == 0.0

    def test_contextual_modifiers(self):
        base = get_recovery_rate(FailureCode.INSUFFICIENT_FUNDS, RecoveryAction.RETRY_DELAYED)
        # High amount penalty
        rate_high = get_contextual_recovery_rate(
            failure_code=FailureCode.INSUFFICIENT_FUNDS,
            action=RecoveryAction.RETRY_DELAYED,
            amount_inr=15000.0,
            hour_of_day=2,
            prior_failed_attempts_30d=4,
        )
        assert rate_high < base

        # Bank hours bonus
        rate_bank = get_contextual_recovery_rate(
            failure_code=FailureCode.INSUFFICIENT_FUNDS,
            action=RecoveryAction.RETRY_DELAYED,
            amount_inr=500.0,
            hour_of_day=11,
            prior_failed_attempts_30d=0,
        )
        assert rate_bank >= base


class TestFeatureExtraction:
    def test_extract_features_vector(self):
        txns = generate_transactions(n=1, random_seed=42)
        fv = extract_features(txns[0])
        assert isinstance(fv, FeatureVector)
        lst = fv.to_list()
        assert len(lst) == len(FEATURE_NAMES)
        assert all(isinstance(x, (int, float)) for x in lst)


class TestModelTrainingAndInference:
    def test_train_predict_and_json_save(self, tmp_path: Path):
        model = RecoveryModel()
        # Train small model for speed in test
        model.train(n_training_txns=200, seed=42)
        assert model._model is not None

        # Test persistence
        save_path = tmp_path / "model.json"
        model.save(save_path)
        assert save_path.exists()

        # Test reload
        new_model = RecoveryModel()
        new_model.load(save_path)
        assert new_model._model is not None

        # Test predict
        txns = generate_transactions(n=2, random_seed=42)
        decision = new_model.predict(txns[0])
        assert decision.recommended_action in MODEL_CANDIDATE_ACTIONS
        assert 0.0 <= decision.confidence <= 1.0
        assert len(decision.shap_top_features) >= 1
        assert "retry_now" in decision.p_recover_by_action


class TestShapAblation:
    def test_run_shap_ablation_output(self):
        from risk_model.shap_ablation import run_shap_ablation
        result = run_shap_ablation(n_background=20, seed=42, verbose=False)
        assert "ranked_features" in result
        assert len(result["ranked_features"]) > 0
        assert "top_feature" in result
        assert 0.0 <= result["top_pct"] <= 100.0
        assert 0.0 <= result["non_failure_code_pct"] <= 100.0


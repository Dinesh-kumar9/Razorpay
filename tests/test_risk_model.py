"""
Unit tests for the risk model, feature extraction, recovery rates, and SHAP explainer.
"""

from __future__ import annotations

from pathlib import Path

from ingestion.generator import generate_transactions
from risk_model.features import FEATURE_NAMES, FeatureVector, extract_features
from risk_model.model import RecoveryModel
from risk_model.recovery_rates import (
    get_contextual_recovery_rate,
    get_recovery_rate,
)
from schemas.decision import MODEL_CANDIDATE_ACTIONS, RecoveryAction
from schemas.transaction import FailureCode


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

    def test_is_outside_business_hours_boundary(self):
        """
        is_outside_business_hours must mirror WINDOW_001 (CONTACT_WINDOW_START_HOUR=9,
        CONTACT_WINDOW_END_HOUR=21). Verify the corrected boundary: hour<9 => outside.

        Regression test for the bug where the feature used hour<8 while the guardrail
        used hour<9 (08:xx transactions were incorrectly marked as 'inside').
        """
        from datetime import UTC, datetime
        from decimal import Decimal

        from schemas.transaction import FailedTransaction, FailureCode, PaymentMethod

        def make_txn_at_hour(hour: int) -> FailedTransaction:
            return FailedTransaction(
                txn_id=f"test-hour-{hour}",
                amount_inr=Decimal("1000"),
                failure_code=FailureCode.INSUFFICIENT_FUNDS,
                payment_method=PaymentMethod.CARD,
                retry_count_so_far=0,
                customer_id="CUST-0001",
                merchant_id="MERCH-001",
                time_of_failure=datetime(2024, 8, 15, hour, 30, 0, tzinfo=UTC),
                gateway_raw_error="DECLINE_51",
                customer_contact_count_24h=0,
            )

        # Hour 8: OUTSIDE (08:xx < 09:00 boundary) — was wrong before the fix
        fv_8 = extract_features(make_txn_at_hour(8))
        assert fv_8.is_outside_business_hours == 1.0, (
            "hour=8 must be classified as OUTSIDE business hours (< 09:00 boundary)"
        )

        # Hour 9: INSIDE (first inside hour)
        fv_9 = extract_features(make_txn_at_hour(9))
        assert fv_9.is_outside_business_hours == 0.0, (
            "hour=9 must be classified as INSIDE business hours (>= 09:00 boundary)"
        )

        # Hour 20: INSIDE (last inside hour)
        fv_20 = extract_features(make_txn_at_hour(20))
        assert fv_20.is_outside_business_hours == 0.0, (
            "hour=20 must be classified as INSIDE business hours (< 21:00 boundary)"
        )

        # Hour 21: OUTSIDE (first outside hour at end)
        fv_21 = extract_features(make_txn_at_hour(21))
        assert fv_21.is_outside_business_hours == 1.0, (
            "hour=21 must be classified as OUTSIDE business hours (>= 21:00 boundary)"
        )


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


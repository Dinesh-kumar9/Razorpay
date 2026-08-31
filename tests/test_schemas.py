"""
Schema contract tests — validates that every schema boundary is correctly defined.

These are not integration tests — they test the schemas in isolation.
If a schema test fails, something fundamental has changed in the data contract.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from schemas.decision import (
    MODEL_CANDIDATE_ACTIONS,
    ModelDecision,
    RecoveryAction,
    SHAPFeature,
)
from schemas.explanation import LLMExplanation
from schemas.transaction import (
    HARD_STOP_CODES,
    NO_RETRY_CODES,
    FailedTransaction,
    FailureCode,
    PaymentMethod,
)


class TestFailedTransaction:
    """FailedTransaction schema validation."""

    def test_valid_transaction_parses(self) -> None:
        txn = FailedTransaction(
            txn_id="TXN-001",
            amount_inr=Decimal("1500.00"),
            failure_code=FailureCode.INSUFFICIENT_FUNDS,
            payment_method=PaymentMethod.UPI,
            retry_count_so_far=0,
            customer_id="CUST-001",
            merchant_id="MERCH-001",
            time_of_failure=datetime.now(tz=timezone.utc),
            gateway_raw_error="BANK_DECLINED: Insufficient funds",
            customer_contact_count_24h=0,
        )
        assert txn.txn_id == "TXN-001"
        assert txn.amount_inr == Decimal("1500.00")

    def test_zero_amount_rejected(self) -> None:
        with pytest.raises(ValidationError):
            FailedTransaction(
                txn_id="TXN-001",
                amount_inr=Decimal("0"),
                failure_code=FailureCode.INSUFFICIENT_FUNDS,
                payment_method=PaymentMethod.UPI,
                retry_count_so_far=0,
                customer_id="CUST-001",
                merchant_id="MERCH-001",
                time_of_failure=datetime.now(tz=timezone.utc),
                gateway_raw_error="error",
                customer_contact_count_24h=0,
            )

    def test_negative_retry_count_rejected(self) -> None:
        with pytest.raises(ValidationError):
            FailedTransaction(
                txn_id="TXN-001",
                amount_inr=Decimal("1000"),
                failure_code=FailureCode.INSUFFICIENT_FUNDS,
                payment_method=PaymentMethod.CARD,
                retry_count_so_far=-1,
                customer_id="CUST-001",
                merchant_id="MERCH-001",
                time_of_failure=datetime.now(tz=timezone.utc),
                gateway_raw_error="error",
                customer_contact_count_24h=0,
            )

    def test_hard_stop_codes_set_correct(self) -> None:
        hard_stops = {
            FailureCode.CARD_BLOCKED,
            FailureCode.FRAUD_FLAG,
            FailureCode.KYC_HOLD,
            FailureCode.STOLEN_CARD,
        }
        assert HARD_STOP_CODES == hard_stops

    def test_no_retry_codes_set_correct(self) -> None:
        no_retry = {FailureCode.CARD_EXPIRED, FailureCode.INVALID_CARD}
        assert NO_RETRY_CODES == no_retry


class TestLLMExplanation:
    """LLMExplanation schema — field length limits."""

    def test_rationale_too_long_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LLMExplanation(
                rationale="x" * 601,
                confidence_caveat="valid caveat",
                fallback_if_wrong="valid fallback",
            )

    def test_caveat_too_long_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LLMExplanation(
                rationale="valid rationale",
                confidence_caveat="x" * 351,
                fallback_if_wrong="valid fallback",
            )

    def test_source_defaults_to_llm(self) -> None:
        exp = LLMExplanation(
            rationale="The payment failed due to insufficient funds.",
            confidence_caveat="Not guaranteed.",
            fallback_if_wrong="Will retry later.",
        )
        assert exp.source == "llm"

    def test_template_source_accepted(self) -> None:
        exp = LLMExplanation(
            rationale="Template rationale.",
            confidence_caveat="Template caveat.",
            fallback_if_wrong="Template fallback.",
            source="template",
        )
        assert exp.source == "template"


class TestModelDecision:
    """ModelDecision schema."""

    def test_shap_features_min_length(self) -> None:
        """At least 1 SHAP feature is required."""
        with pytest.raises(ValidationError):
            ModelDecision(
                txn_id="TXN-001",
                recommended_action=RecoveryAction.RETRY_NOW,
                confidence=0.75,
                shap_top_features=[],  # violates min_length=1
            )

    def test_confidence_out_of_range_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ModelDecision(
                txn_id="TXN-001",
                recommended_action=RecoveryAction.RETRY_NOW,
                confidence=1.5,  # > 1.0
                shap_top_features=[
                    SHAPFeature(
                        feature_name="f", shap_value=0.1, feature_value="v", direction="positive"
                    )
                ],
            )

    def test_model_candidate_actions_excludes_stop(self) -> None:
        """STOP is a policy-only action; model should never recommend it."""
        assert RecoveryAction.STOP not in MODEL_CANDIDATE_ACTIONS
        assert len(MODEL_CANDIDATE_ACTIONS) == 4

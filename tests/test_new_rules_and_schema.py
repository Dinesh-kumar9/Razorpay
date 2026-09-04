"""
Tests for new additions -- OPT_OUT_001, COST_001 guardrail rules
and schema fields customer_opted_out / recovery_cost_inr / customer_message_hinglish.

Test organisation:
  TestOptOut001       -- customer_opted_out=True always fires, regardless of action type
  TestCost001         -- recovery_cost_inr > 5% threshold fires; <= threshold does not
  TestHinglishField   -- LLMExplanation.customer_message_hinglish schema validation
  TestFallbackHinglish -- deterministic fallback populates Hinglish correctly per action
  TestNewFieldDefaults -- new FailedTransaction fields default safely (no existing txns break)
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from llm_layer.fallback import get_fallback_explanation
from policy_engine.engine import PolicyEngine
from policy_engine.rules import COST_THRESHOLD_PCT, check_COST_001, check_OPT_OUT_001
from schemas.decision import RecoveryAction
from schemas.explanation import LLMExplanation
from schemas.transaction import FailedTransaction, FailureCode
from tests.conftest import make_model_decision, make_txn


# ---- TestOptOut001 -----------------------------------------------------------

class TestOptOut001:
    """OPT_OUT_001: opted-out customer halts ALL automated recovery unconditionally."""

    @pytest.mark.parametrize(
        "proposed_action",
        [
            RecoveryAction.RETRY_NOW,
            RecoveryAction.RETRY_DELAYED,
            RecoveryAction.NUDGE_ALT_METHOD,
            RecoveryAction.ESCALATE_TO_HUMAN,
        ],
    )
    def test_opted_out_always_stops_regardless_of_action(
        self, proposed_action: RecoveryAction
    ) -> None:
        txn = make_txn(customer_opted_out=True)
        result = check_OPT_OUT_001(txn, proposed_action)
        assert result is not None
        assert result.rule_id == "OPT_OUT_001"
        assert result.override_action == RecoveryAction.STOP

    def test_opted_out_false_does_not_fire(self) -> None:
        txn = make_txn(customer_opted_out=False)
        result = check_OPT_OUT_001(txn, RecoveryAction.RETRY_NOW)
        assert result is None

    def test_opted_out_overrides_model_via_engine(self) -> None:
        eng = PolicyEngine()
        txn = make_txn(customer_opted_out=True)
        decision = eng.evaluate(txn, make_model_decision(txn, action=RecoveryAction.RETRY_NOW))
        assert decision.final_action == RecoveryAction.STOP
        assert decision.guardrail_rule_id == "OPT_OUT_001"
        assert decision.was_overridden is True
        assert decision.rule_mandated is True

    def test_opted_out_reason_mentions_dpdp(self) -> None:
        txn = make_txn(customer_opted_out=True)
        result = check_OPT_OUT_001(txn, RecoveryAction.RETRY_NOW)
        assert result is not None
        assert "DPDP" in result.reason

    def test_opted_out_reason_mentions_customer_id(self) -> None:
        txn = make_txn(customer_opted_out=True)
        result = check_OPT_OUT_001(txn, RecoveryAction.RETRY_NOW)
        assert result is not None
        assert txn.customer_id in result.reason

    def test_hard_stop_takes_priority_over_opt_out(self) -> None:
        """
        HARD_STOP_001 fires BEFORE OPT_OUT_001 (see ADR 0008).

        Rationale: ESCALATE_TO_HUMAN is an internal compliance routing action,
        not automated recovery contact to the customer. DPDP consent revocation
        (DPDP Act 2023, Chapter III) governs commercial automated contact to the
        data principal — it does not apply to mandatory RBI fraud escalation.
        A customer cannot opt out of having a stolen-card/fraud-flag case
        escalated to human fraud review.
        """
        eng = PolicyEngine()
        txn = make_txn(failure_code=FailureCode.CARD_BLOCKED, customer_opted_out=True)
        decision = eng.evaluate(txn, make_model_decision(txn, action=RecoveryAction.RETRY_NOW))
        # HARD_STOP_001 must win — statutory RBI obligation supersedes DPDP consent
        assert decision.guardrail_rule_id == "HARD_STOP_001"
        assert decision.final_action == RecoveryAction.ESCALATE_TO_HUMAN

    def test_opted_out_fires_for_non_hard_stop_code(self) -> None:
        """OPT_OUT_001 fires correctly for soft-decline codes (no HARD_STOP_001 competition)."""
        eng = PolicyEngine()
        txn = make_txn(failure_code=FailureCode.INSUFFICIENT_FUNDS, customer_opted_out=True)
        decision = eng.evaluate(txn, make_model_decision(txn, action=RecoveryAction.RETRY_NOW))
        assert decision.guardrail_rule_id == "OPT_OUT_001"
        assert decision.final_action == RecoveryAction.STOP


# ---- TestCost001 -------------------------------------------------------------

class TestCost001:
    """COST_001: stops retry when cumulative cost exceeds 5% of transaction amount."""

    def test_zero_cost_does_not_fire(self) -> None:
        txn = make_txn(amount=Decimal("1000.00"), recovery_cost_inr=Decimal("0"))
        result = check_COST_001(txn, RecoveryAction.RETRY_NOW)
        assert result is None

    def test_cost_below_threshold_does_not_fire(self) -> None:
        txn = make_txn(amount=Decimal("1000.00"), recovery_cost_inr=Decimal("49.99"))
        result = check_COST_001(txn, RecoveryAction.RETRY_NOW)
        assert result is None

    def test_cost_exactly_at_threshold_does_not_fire(self) -> None:
        txn = make_txn(amount=Decimal("1000.00"), recovery_cost_inr=Decimal("50.00"))
        result = check_COST_001(txn, RecoveryAction.RETRY_NOW)
        assert result is None

    def test_cost_above_threshold_fires_retry_now(self) -> None:
        txn = make_txn(amount=Decimal("1000.00"), recovery_cost_inr=Decimal("51.00"))
        result = check_COST_001(txn, RecoveryAction.RETRY_NOW)
        assert result is not None
        assert result.rule_id == "COST_001"
        assert result.override_action == RecoveryAction.STOP

    def test_cost_above_threshold_fires_retry_delayed(self) -> None:
        txn = make_txn(amount=Decimal("500.00"), recovery_cost_inr=Decimal("30.00"))
        result = check_COST_001(txn, RecoveryAction.RETRY_DELAYED)
        assert result is not None
        assert result.rule_id == "COST_001"

    def test_cost_above_threshold_does_not_block_nudge(self) -> None:
        txn = make_txn(amount=Decimal("1000.00"), recovery_cost_inr=Decimal("100.00"))
        result = check_COST_001(txn, RecoveryAction.NUDGE_ALT_METHOD)
        assert result is None

    def test_cost_above_threshold_does_not_block_escalate(self) -> None:
        txn = make_txn(amount=Decimal("1000.00"), recovery_cost_inr=Decimal("100.00"))
        result = check_COST_001(txn, RecoveryAction.ESCALATE_TO_HUMAN)
        assert result is None

    def test_cost001_reason_mentions_amounts(self) -> None:
        txn = make_txn(amount=Decimal("200.00"), recovery_cost_inr=Decimal("15.00"))
        result = check_COST_001(txn, RecoveryAction.RETRY_NOW)
        assert result is not None
        assert "15.00" in result.reason
        assert "200.00" in result.reason

    def test_cost001_via_engine_produces_stop(self) -> None:
        eng = PolicyEngine()
        txn = make_txn(
            failure_code=FailureCode.INSUFFICIENT_FUNDS,
            amount=Decimal("500.00"),
            recovery_cost_inr=Decimal("30.00"),
        )
        decision = eng.evaluate(txn, make_model_decision(txn, action=RecoveryAction.RETRY_NOW))
        assert decision.final_action == RecoveryAction.STOP
        assert decision.guardrail_rule_id == "COST_001"
        assert decision.was_overridden is True

    def test_cost_threshold_constant_is_five_percent(self) -> None:
        assert COST_THRESHOLD_PCT == 0.05


# ---- TestHinglishField -------------------------------------------------------

class TestHinglishField:
    """Schema-level tests for the customer_message_hinglish field."""

    def test_hinglish_field_defaults_to_none(self) -> None:
        exp = LLMExplanation(
            rationale="Payment failed.",
            confidence_caveat="Not guaranteed.",
            fallback_if_wrong="Will retry.",
        )
        assert exp.customer_message_hinglish is None

    def test_hinglish_field_accepts_none(self) -> None:
        exp = LLMExplanation(
            rationale="Payment failed.",
            confidence_caveat="Not guaranteed.",
            fallback_if_wrong="Will retry.",
            customer_message_hinglish=None,
        )
        assert exp.customer_message_hinglish is None

    def test_hinglish_field_accepts_valid_string(self) -> None:
        msg = "Namaste! Aapka payment fail ho gaya."
        exp = LLMExplanation(
            rationale="Payment failed.",
            confidence_caveat="Not guaranteed.",
            fallback_if_wrong="Will retry.",
            customer_message_hinglish=msg,
        )
        assert exp.customer_message_hinglish == msg

    def test_hinglish_field_too_long_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LLMExplanation(
                rationale="Payment failed.",
                confidence_caveat="Not guaranteed.",
                fallback_if_wrong="Will retry.",
                customer_message_hinglish="x" * 301,
            )

    def test_hinglish_at_exactly_300_chars_accepted(self) -> None:
        exp = LLMExplanation(
            rationale="Payment failed.",
            confidence_caveat="Not guaranteed.",
            fallback_if_wrong="Will retry.",
            customer_message_hinglish="x" * 300,
        )
        assert len(exp.customer_message_hinglish) == 300  # type: ignore[arg-type]


# ---- TestFallbackHinglish ----------------------------------------------------

class TestFallbackHinglish:
    """Deterministic fallback templates produce correct Hinglish messages per action."""

    def _policy_decision_for(self, action: RecoveryAction) -> object:
        from schemas.decision import PolicyDecision
        return PolicyDecision(
            txn_id="TXN-TEST",
            final_action=action,
            model_action=action,
            was_overridden=False,
            rule_mandated=False,
        )

    def test_retry_now_produces_hinglish(self) -> None:
        from schemas.decision import SHAPFeature
        pd = self._policy_decision_for(RecoveryAction.RETRY_NOW)
        feats = [SHAPFeature(feature_name="retry_count", shap_value=0.3, feature_value="0", direction="positive")]
        exp = get_fallback_explanation(pd, feats, "insufficient_funds", amount_inr=1500.0)  # type: ignore[arg-type]
        assert exp.customer_message_hinglish is not None
        assert "1500" in exp.customer_message_hinglish
        assert "Namaste" in exp.customer_message_hinglish

    def test_retry_delayed_produces_hinglish(self) -> None:
        from schemas.decision import SHAPFeature
        pd = self._policy_decision_for(RecoveryAction.RETRY_DELAYED)
        feats = [SHAPFeature(feature_name="retry_count", shap_value=0.3, feature_value="1", direction="positive")]
        exp = get_fallback_explanation(pd, feats, "do_not_honor", amount_inr=2000.0)  # type: ignore[arg-type]
        assert exp.customer_message_hinglish is not None
        assert "2000" in exp.customer_message_hinglish

    def test_nudge_produces_hinglish_with_payment_link(self) -> None:
        from schemas.decision import SHAPFeature
        pd = self._policy_decision_for(RecoveryAction.NUDGE_ALT_METHOD)
        feats = [SHAPFeature(feature_name="payment_method", shap_value=0.2, feature_value="card", direction="negative")]
        exp = get_fallback_explanation(pd, feats, "card_expired", amount_inr=750.0)  # type: ignore[arg-type]
        assert exp.customer_message_hinglish is not None
        assert "pay.razorpay.com/retry" in exp.customer_message_hinglish

    def test_escalate_produces_none_hinglish(self) -> None:
        from schemas.decision import SHAPFeature
        pd = self._policy_decision_for(RecoveryAction.ESCALATE_TO_HUMAN)
        feats = [SHAPFeature(feature_name="failure_code", shap_value=0.5, feature_value="fraud_flag", direction="positive")]
        exp = get_fallback_explanation(pd, feats, "fraud_flag", amount_inr=5000.0)  # type: ignore[arg-type]
        assert exp.customer_message_hinglish is None

    def test_stop_produces_none_hinglish(self) -> None:
        from schemas.decision import SHAPFeature
        pd = self._policy_decision_for(RecoveryAction.STOP)
        feats = [SHAPFeature(feature_name="retry_count", shap_value=0.6, feature_value="3", direction="positive")]
        exp = get_fallback_explanation(pd, feats, "insufficient_funds", amount_inr=300.0)  # type: ignore[arg-type]
        assert exp.customer_message_hinglish is None

    def test_hinglish_message_within_max_length(self) -> None:
        from schemas.decision import SHAPFeature
        feats = [SHAPFeature(feature_name="retry_count", shap_value=0.3, feature_value="0", direction="positive")]
        for action in [RecoveryAction.RETRY_NOW, RecoveryAction.RETRY_DELAYED, RecoveryAction.NUDGE_ALT_METHOD]:
            pd = self._policy_decision_for(action)
            exp = get_fallback_explanation(pd, feats, "insufficient_funds", amount_inr=50000.0)  # type: ignore[arg-type]
            if exp.customer_message_hinglish is not None:
                assert len(exp.customer_message_hinglish) <= 300


# ---- TestNewFieldDefaults ----------------------------------------------------

class TestNewFieldDefaults:
    """New FailedTransaction fields must have safe defaults so existing txns are unaffected."""

    def test_customer_opted_out_defaults_false(self) -> None:
        txn = make_txn()
        assert txn.customer_opted_out is False

    def test_recovery_cost_inr_defaults_zero(self) -> None:
        txn = make_txn()
        assert txn.recovery_cost_inr == Decimal("0")

    def test_negative_recovery_cost_rejected(self) -> None:
        from datetime import UTC, datetime
        from schemas.transaction import PaymentMethod
        with pytest.raises(ValidationError):
            FailedTransaction(
                txn_id="TXN-001",
                amount_inr=Decimal("1000"),
                failure_code=FailureCode.INSUFFICIENT_FUNDS,
                payment_method=PaymentMethod.CARD,
                retry_count_so_far=0,
                customer_id="CUST-001",
                merchant_id="MERCH-001",
                time_of_failure=datetime.now(tz=UTC),
                gateway_raw_error="err",
                customer_contact_count_24h=0,
                recovery_cost_inr=Decimal("-1"),
            )

    def test_existing_make_txn_still_works_with_no_new_fields(self) -> None:
        txn = make_txn(failure_code=FailureCode.INSUFFICIENT_FUNDS, retry_count=0)
        assert txn.customer_opted_out is False
        assert txn.recovery_cost_inr == Decimal("0")

    def test_opted_out_true_accepted(self) -> None:
        txn = make_txn(customer_opted_out=True)
        assert txn.customer_opted_out is True

    def test_recovery_cost_inr_positive_accepted(self) -> None:
        txn = make_txn(recovery_cost_inr=Decimal("25.50"))
        assert txn.recovery_cost_inr == Decimal("25.50")

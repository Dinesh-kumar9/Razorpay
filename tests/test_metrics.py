"""
Test suite for simulation/metrics.py.

Verifies:
  1. Adversarial false-escalation metric increments when model recommends
     ESCALATE_TO_HUMAN for non-hard-stop failure codes (e.g. INSUFFICIENT_FUNDS, NETWORK_TIMEOUT).
  2. False-escalation does NOT increment for genuine hard stops (e.g. FRAUD_FLAG, CARD_BLOCKED).
  3. Metric calculations (uplift, stopping-rule violations, override rates).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from schemas.audit import AuditRecord, SimulatedOutcome
from schemas.decision import RecoveryAction
from schemas.explanation import LLMExplanation
from schemas.transaction import FailureCode, PaymentMethod
from simulation.metrics import compute_metrics
from tests.conftest import make_txn


def _create_audit_record(
    failure_code: FailureCode,
    model_action: RecoveryAction,
    final_action: RecoveryAction,
    amount_inr: Decimal = Decimal("2500"),
    was_overridden: bool = False,
    recovered: bool = False,
) -> AuditRecord:
    """Helper to construct an AuditRecord with explicit model and final actions."""
    txn = make_txn(failure_code=failure_code, amount=amount_inr)
    return AuditRecord(
        txn_id=txn.txn_id,
        timestamp=datetime.now(tz=UTC),
        amount_inr=amount_inr,
        failure_code=failure_code,
        payment_method=PaymentMethod.UPI,
        customer_id=txn.customer_id,
        merchant_id=txn.merchant_id,
        model_action=model_action,
        model_confidence=0.85,
        final_action=final_action,
        was_overridden=was_overridden,
        override_reason=None,
        guardrail_rule_id=None,
        retry_delay_minutes=None,
        explanation=LLMExplanation(
            rationale="Test explanation rationale.",
            confidence_caveat="Test confidence caveat.",
            fallback_if_wrong="Test fallback action.",
            source="template",
        ),
        simulated_outcome=SimulatedOutcome(
            recovered=recovered,
            recovery_probability_used=0.25,
            amount_recovered_inr=amount_inr if recovered else Decimal("0"),
        ),
        amount_recovered_inr=amount_inr if recovered else Decimal("0"),
    )


class TestMetricsFalseEscalation:
    """Adversarial and functional validation of false_escalation_count."""

    def test_false_escalation_increments_for_insufficient_funds(self) -> None:
        """
        Adversarial test: model suggests ESCALATE_TO_HUMAN for INSUFFICIENT_FUNDS (soft decline).
        compute_metrics must identify this as a false escalation and increment count to 1.
        """
        record = _create_audit_record(
            failure_code=FailureCode.INSUFFICIENT_FUNDS,
            model_action=RecoveryAction.ESCALATE_TO_HUMAN,
            final_action=RecoveryAction.ESCALATE_TO_HUMAN,
        )

        metrics = compute_metrics(
            records=[record],
            recovered_blind_retry=Decimal("500"),
            recovered_naive_multi_retry=Decimal("1000"),
            seed=42,
        )

        assert metrics.false_escalation_count == 1
        assert metrics.false_escalation_rate_pct == pytest.approx(100.0)

    def test_false_escalation_increments_for_network_timeout(self) -> None:
        """
        Adversarial test: model suggests ESCALATE_TO_HUMAN for NETWORK_TIMEOUT (technical retryable).
        compute_metrics must increment false_escalation_count.
        """
        record = _create_audit_record(
            failure_code=FailureCode.NETWORK_TIMEOUT,
            model_action=RecoveryAction.ESCALATE_TO_HUMAN,
            final_action=RecoveryAction.ESCALATE_TO_HUMAN,
        )

        metrics = compute_metrics(
            records=[record],
            recovered_blind_retry=Decimal("500"),
            recovered_naive_multi_retry=Decimal("1000"),
            seed=42,
        )

        assert metrics.false_escalation_count == 1
        assert metrics.false_escalation_rate_pct == pytest.approx(100.0)

    def test_false_escalation_zero_for_genuine_hard_stop(self) -> None:
        """
        Genuine hard stop (FRAUD_FLAG): model suggests ESCALATE_TO_HUMAN.
        This is a correct escalation, so false_escalation_count must be 0.
        """
        record = _create_audit_record(
            failure_code=FailureCode.FRAUD_FLAG,
            model_action=RecoveryAction.ESCALATE_TO_HUMAN,
            final_action=RecoveryAction.ESCALATE_TO_HUMAN,
        )

        metrics = compute_metrics(
            records=[record],
            recovered_blind_retry=Decimal("0"),
            recovered_naive_multi_retry=Decimal("0"),
            seed=42,
        )

        assert metrics.false_escalation_count == 0
        assert metrics.false_escalation_rate_pct == 0.0

    def test_mixed_batch_false_escalation_rate(self) -> None:
        """
        Mixed batch: 1 soft false escalation + 1 legitimate hard escalation + 2 auto retries.
        false_escalation_count should be exactly 1 out of 4 (25.0%).
        """
        records = [
            _create_audit_record(FailureCode.INSUFFICIENT_FUNDS, RecoveryAction.ESCALATE_TO_HUMAN, RecoveryAction.ESCALATE_TO_HUMAN),
            _create_audit_record(FailureCode.FRAUD_FLAG, RecoveryAction.ESCALATE_TO_HUMAN, RecoveryAction.ESCALATE_TO_HUMAN),
            _create_audit_record(FailureCode.INSUFFICIENT_FUNDS, RecoveryAction.RETRY_DELAYED, RecoveryAction.RETRY_DELAYED, recovered=True),
            _create_audit_record(FailureCode.NETWORK_TIMEOUT, RecoveryAction.RETRY_NOW, RecoveryAction.RETRY_NOW, recovered=True),
        ]

        metrics = compute_metrics(
            records=records,
            recovered_blind_retry=Decimal("2500"),
            recovered_naive_multi_retry=Decimal("4000"),
            seed=42,
        )

        assert metrics.false_escalation_count == 1
        assert metrics.false_escalation_rate_pct == pytest.approx(25.0)
        assert metrics.total_transactions == 4


class TestUpliftCalculationsAndBaselines:
    """Rigorous mathematical tests for dual/multi-baseline uplift calculations."""

    def test_single_retry_uplift_uses_single_retry_denominator(self) -> None:
        """
        uplift_vs_blind_retry_pct must calculate:
        ((agent_recovered - blind_recovered) / blind_recovered) * 100
        """
        record = _create_audit_record(
            failure_code=FailureCode.INSUFFICIENT_FUNDS,
            model_action=RecoveryAction.RETRY_NOW,
            final_action=RecoveryAction.RETRY_NOW,
            amount_inr=Decimal("10000"),
            recovered=True,
        )
        blind = Decimal("2000")
        constrained = Decimal("5000")
        unconstrained = Decimal("8000")

        metrics = compute_metrics(
            records=[record],
            recovered_blind_retry=blind,
            recovered_naive_multi_retry=unconstrained,
            seed=42,
            recovered_constrained_multi_retry=constrained,
        )

        expected_single_uplift = float((Decimal("10000") - blind) / blind * 100)
        assert metrics.uplift_vs_blind_retry_pct == pytest.approx(expected_single_uplift, rel=1e-3)
        assert metrics.uplift_vs_blind_retry_pct == pytest.approx(400.0)

    def test_constrained_multi_retry_uplift_uses_constrained_denominator(self) -> None:
        """
        uplift_vs_constrained_multi_retry_pct must calculate:
        ((agent_recovered - constrained_recovered) / constrained_recovered) * 100
        """
        record = _create_audit_record(
            failure_code=FailureCode.INSUFFICIENT_FUNDS,
            model_action=RecoveryAction.RETRY_NOW,
            final_action=RecoveryAction.RETRY_NOW,
            amount_inr=Decimal("10000"),
            recovered=True,
        )
        blind = Decimal("2000")
        constrained = Decimal("5000")
        unconstrained = Decimal("8000")

        metrics = compute_metrics(
            records=[record],
            recovered_blind_retry=blind,
            recovered_naive_multi_retry=unconstrained,
            seed=42,
            recovered_constrained_multi_retry=constrained,
        )

        expected_constrained_uplift = float((Decimal("10000") - constrained) / constrained * 100)
        assert metrics.uplift_vs_constrained_multi_retry_pct == pytest.approx(expected_constrained_uplift, rel=1e-3)
        assert metrics.uplift_vs_constrained_multi_retry_pct == pytest.approx(100.0)

    def test_different_baselines_cannot_reuse_same_uplift(self) -> None:
        """
        Verify that single-retry, constrained multi-retry, and unconstrained multi-retry
        calculate independent uplifts and cannot accidentally share the same value.
        """
        record = _create_audit_record(
            failure_code=FailureCode.INSUFFICIENT_FUNDS,
            model_action=RecoveryAction.RETRY_NOW,
            final_action=RecoveryAction.RETRY_NOW,
            amount_inr=Decimal("10000"),
            recovered=True,
        )
        blind = Decimal("2000")          # +400.0% uplift
        constrained = Decimal("5000")    # +100.0% uplift
        unconstrained = Decimal("12500") # -20.0% uplift

        metrics = compute_metrics(
            records=[record],
            recovered_blind_retry=blind,
            recovered_naive_multi_retry=unconstrained,
            seed=42,
            recovered_constrained_multi_retry=constrained,
            unconstrained_violations={"test_violation": 42},
        )

        assert metrics.uplift_vs_blind_retry_pct == pytest.approx(400.0)
        assert metrics.uplift_vs_constrained_multi_retry_pct == pytest.approx(100.0)
        assert metrics.uplift_vs_naive_multi_retry_pct == pytest.approx(-20.0)
        assert metrics.unconstrained_violations_total == 42
        # Ensure all three uplifts are distinct
        assert len({metrics.uplift_vs_blind_retry_pct, metrics.uplift_vs_constrained_multi_retry_pct, metrics.uplift_vs_naive_multi_retry_pct}) == 3

    def test_zero_baseline_avoids_division_by_zero(self) -> None:
        record = _create_audit_record(
            failure_code=FailureCode.INSUFFICIENT_FUNDS,
            model_action=RecoveryAction.RETRY_NOW,
            final_action=RecoveryAction.RETRY_NOW,
            amount_inr=Decimal("10000"),
            recovered=True,
        )
        metrics = compute_metrics(
            records=[record],
            recovered_blind_retry=Decimal("0"),
            recovered_naive_multi_retry=Decimal("0"),
            seed=42,
            recovered_constrained_multi_retry=Decimal("0"),
        )
        assert metrics.uplift_vs_blind_retry_pct == 0.0
        assert metrics.uplift_vs_constrained_multi_retry_pct == 0.0
        assert metrics.uplift_vs_naive_multi_retry_pct == 0.0

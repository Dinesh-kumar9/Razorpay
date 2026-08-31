"""
Adversarial test: false-escalation increment.

This test asserts that false_escalation_count increments CORRECTLY when
the model recommends escalation for a non-hard-stop failure code (e.g.,
INSUFFICIENT_FUNDS or NETWORK_TIMEOUT — codes where auto-recovery is possible).

"False escalation" is defined as: model recommends ESCALATE_TO_HUMAN for a
failure code NOT in HARD_STOP_CODES. The policy engine may still allow this
escalation (no guardrail explicitly blocks escalation of soft codes), but the
metrics layer MUST record it as a false escalation to be transparent about
the model's over-caution rate.

WHY THIS MATTERS: The PRD requires "honest metrics including false-positive
cost." If this count were silently dropped or computed incorrectly, we'd be
reporting a misleadingly clean agent. This test is the audit-trail guarantee
that the metric is computed correctly.

Audit citation: simulation/metrics.py lines 67-69.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from schemas.audit import AuditRecord, BatchMetrics
from schemas.decision import RecoveryAction
from schemas.transaction import FailureCode, HARD_STOP_CODES
from simulation.metrics import compute_metrics
from tests.conftest import make_txn


def _minimal_audit_record(
    txn,
    model_action: RecoveryAction,
    final_action: RecoveryAction,
    was_overridden: bool = False,
    recovered: bool = False,
) -> AuditRecord:
    """Build a minimal AuditRecord for metrics testing."""
    from datetime import datetime, timezone
    from schemas.audit import SimulatedOutcome
    from schemas.explanation import LLMExplanation

    amount = txn.amount_inr
    return AuditRecord(
        txn_id=txn.txn_id,
        timestamp=datetime.now(tz=timezone.utc),
        amount_inr=amount,
        failure_code=txn.failure_code,
        payment_method=txn.payment_method,
        customer_id=txn.customer_id,
        merchant_id=txn.merchant_id,
        model_action=model_action,
        model_confidence=0.75,
        final_action=final_action,
        was_overridden=was_overridden,
        override_reason=None,
        guardrail_rule_id=None,
        retry_delay_minutes=None,
        explanation=LLMExplanation(
            rationale="Test rationale for adversarial test.",
            confidence_caveat="Test caveat — no real uncertainty.",
            fallback_if_wrong="Test fallback action description.",
            source="template",
        ),
        simulated_outcome=SimulatedOutcome(
            recovered=recovered,
            recovery_probability_used=0.30,
            amount_recovered_inr=amount if recovered else Decimal("0"),
        ),
        amount_recovered_inr=amount if recovered else Decimal("0"),
    )


class TestFalseEscalationMetric:
    """
    Adversarial test suite for the false_escalation_count metric.

    These tests assert the metric increments correctly for every combination
    of model recommendation and failure code category.
    """

    def _run_metrics(self, records: list[AuditRecord]) -> BatchMetrics:
        """Helper: compute metrics against two trivial baselines."""
        return compute_metrics(
            records=records,
            recovered_blind_retry=Decimal("1000"),
            recovered_naive_multi_retry=Decimal("2000"),
            seed=42,
        )

    def test_false_escalation_increments_for_soft_decline(self):
        """
        ADVERSARIAL: model escalates INSUFFICIENT_FUNDS (not in HARD_STOP_CODES).
        The policy engine does NOT override this (escalation is permitted),
        so model_action == ESCALATE_TO_HUMAN for a non-hard-stop code.
        false_escalation_count MUST be 1.
        """
        assert FailureCode.INSUFFICIENT_FUNDS not in HARD_STOP_CODES, \
            "Test precondition: INSUFFICIENT_FUNDS must be a soft code, not hard-stop"

        txn = make_txn(failure_code=FailureCode.INSUFFICIENT_FUNDS, retry_count=0)
        record = _minimal_audit_record(
            txn,
            model_action=RecoveryAction.ESCALATE_TO_HUMAN,
            final_action=RecoveryAction.ESCALATE_TO_HUMAN,
            was_overridden=False,
            recovered=False,
        )
        metrics = self._run_metrics([record])
        assert metrics.false_escalation_count == 1, (
            f"Expected false_escalation_count=1 for INSUFFICIENT_FUNDS escalation, "
            f"got {metrics.false_escalation_count}"
        )
        assert metrics.false_escalation_rate_pct == pytest.approx(100.0, abs=0.01)

    def test_false_escalation_increments_for_network_timeout(self):
        """
        ADVERSARIAL: model escalates NETWORK_TIMEOUT — a transient system error
        that should be retried, not escalated. Must count as false escalation.
        """
        assert FailureCode.NETWORK_TIMEOUT not in HARD_STOP_CODES, \
            "Test precondition: NETWORK_TIMEOUT must be a soft code"

        txn = make_txn(failure_code=FailureCode.NETWORK_TIMEOUT, retry_count=0)
        record = _minimal_audit_record(
            txn,
            model_action=RecoveryAction.ESCALATE_TO_HUMAN,
            final_action=RecoveryAction.ESCALATE_TO_HUMAN,
            was_overridden=False,
            recovered=False,
        )
        metrics = self._run_metrics([record])
        assert metrics.false_escalation_count == 1

    def test_no_false_escalation_for_hard_stop_code(self):
        """
        CONTROL: model escalates FRAUD_FLAG (in HARD_STOP_CODES).
        This is CORRECT behaviour, not a false escalation.
        false_escalation_count MUST be 0.
        """
        assert FailureCode.FRAUD_FLAG in HARD_STOP_CODES, \
            "Test precondition: FRAUD_FLAG must be in HARD_STOP_CODES"

        txn = make_txn(failure_code=FailureCode.FRAUD_FLAG)
        record = _minimal_audit_record(
            txn,
            model_action=RecoveryAction.ESCALATE_TO_HUMAN,
            final_action=RecoveryAction.ESCALATE_TO_HUMAN,
            was_overridden=True,  # policy engine correctly mandates escalation
            recovered=False,
        )
        metrics = self._run_metrics([record])
        assert metrics.false_escalation_count == 0, (
            f"Escalating a HARD_STOP code must NOT count as false escalation, "
            f"got {metrics.false_escalation_count}"
        )

    def test_false_escalation_count_accumulates_across_multiple_records(self):
        """
        ADVERSARIAL: 3 soft-code escalations + 1 hard-stop escalation.
        false_escalation_count MUST be 3, not 4.
        """
        soft_codes = [
            FailureCode.INSUFFICIENT_FUNDS,
            FailureCode.DO_NOT_HONOR,
            FailureCode.GATEWAY_ERROR,
        ]
        hard_code = FailureCode.STOLEN_CARD

        records = []
        for fc in soft_codes:
            txn = make_txn(failure_code=fc)
            records.append(_minimal_audit_record(
                txn,
                model_action=RecoveryAction.ESCALATE_TO_HUMAN,
                final_action=RecoveryAction.ESCALATE_TO_HUMAN,
            ))

        # Hard stop escalation — should NOT count
        hard_txn = make_txn(failure_code=hard_code)
        records.append(_minimal_audit_record(
            hard_txn,
            model_action=RecoveryAction.ESCALATE_TO_HUMAN,
            final_action=RecoveryAction.ESCALATE_TO_HUMAN,
            was_overridden=True,
        ))

        metrics = self._run_metrics(records)
        assert metrics.false_escalation_count == 3, (
            f"Expected 3 false escalations (3 soft + 1 hard), got {metrics.false_escalation_count}"
        )
        assert metrics.false_escalation_rate_pct == pytest.approx(75.0, abs=0.01)

    def test_no_false_escalation_when_model_did_not_escalate(self):
        """
        CONTROL: model recommends RETRY_NOW (not escalation).
        Even if final_action happens to be escalation (policy override),
        false_escalation_count is keyed on model_action, not final_action.
        Must be 0.
        """
        txn = make_txn(failure_code=FailureCode.INSUFFICIENT_FUNDS)
        record = _minimal_audit_record(
            txn,
            model_action=RecoveryAction.RETRY_NOW,     # model said retry
            final_action=RecoveryAction.ESCALATE_TO_HUMAN,  # policy overrode
            was_overridden=True,
        )
        metrics = self._run_metrics([record])
        assert metrics.false_escalation_count == 0, (
            "false_escalation_count should be keyed on model_action, not final_action"
        )

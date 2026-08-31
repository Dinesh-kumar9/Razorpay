"""
Unit tests for audit logger: persistence, parameterized SQL queries, pagination, and record counts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from audit.logger import AuditLogger
from schemas.audit import AuditRecord, SimulatedOutcome
from schemas.decision import RecoveryAction
from schemas.explanation import LLMExplanation
from schemas.transaction import FailureCode, PaymentMethod


@pytest.fixture
def temp_audit_logger(tmp_path: Path) -> AuditLogger:
    db_path = tmp_path / "test_audit.db"
    return AuditLogger(db_path=db_path)


def test_log_and_query_record(temp_audit_logger: AuditLogger):
    rec = AuditRecord(
        txn_id="TXN-AUDIT-TEST-001",
        timestamp=datetime.now(UTC),
        merchant_id="merch_123",
        customer_id="cust_456",
        payment_method=PaymentMethod.UPI,
        amount_inr=Decimal("1500.00"),
        failure_code=FailureCode.INSUFFICIENT_FUNDS,
        model_action=RecoveryAction.RETRY_DELAYED,
        model_confidence=0.85,
        final_action=RecoveryAction.RETRY_DELAYED,
        was_overridden=False,
        override_reason=None,
        guardrail_rule_id=None,
        retry_delay_minutes=1440,
        explanation=LLMExplanation(
            rationale="Delayed retry recommended for salary cycle top up.",
            confidence_caveat="Depends on balance replenishment.",
            fallback_if_wrong="Prompt for alternate UPI payment.",
            source="llm",
        ),
        simulated_outcome=SimulatedOutcome(
            recovered=True,
            recovery_probability_used=0.42,
            amount_recovered_inr=Decimal("1500.00"),
        ),
        amount_recovered_inr=Decimal("1500.00"),
    )

    temp_audit_logger.log(rec)
    assert temp_audit_logger.count() == 1

    fetched = temp_audit_logger.query_by_txn("TXN-AUDIT-TEST-001")
    assert fetched is not None
    assert fetched.txn_id == "TXN-AUDIT-TEST-001"
    assert fetched.amount_inr == Decimal("1500.00")
    assert fetched.final_action == RecoveryAction.RETRY_DELAYED
    assert fetched.explanation.rationale == "Delayed retry recommended for salary cycle top up."


def test_pagination_and_filtering(temp_audit_logger: AuditLogger):
    for i in range(15):
        rec = AuditRecord(
            txn_id=f"TXN-PAG-{i:03d}",
            timestamp=datetime.now(UTC),
            merchant_id="merch_1",
            customer_id="cust_1",
            payment_method=PaymentMethod.CARD,
            amount_inr=Decimal("100.00"),
            failure_code=FailureCode.INSUFFICIENT_FUNDS,
            model_action=RecoveryAction.RETRY_NOW,
            model_confidence=0.5,
            final_action=RecoveryAction.RETRY_NOW if i % 2 == 0 else RecoveryAction.ESCALATE_TO_HUMAN,
            was_overridden=(i % 2 != 0),
            override_reason="Hard stop" if i % 2 != 0 else None,
            guardrail_rule_id="HARD_STOP_001" if i % 2 != 0 else None,
            retry_delay_minutes=None,
            explanation=LLMExplanation(
                rationale="Reason",
                confidence_caveat="Caveat",
                fallback_if_wrong="Fallback",
                source="template",
            ),
            simulated_outcome=SimulatedOutcome(
                recovered=(i % 3 == 0),
                recovery_probability_used=0.5,
                amount_recovered_inr=Decimal("100.00") if (i % 3 == 0) else Decimal("0.00"),
            ),
            amount_recovered_inr=Decimal("100.00") if (i % 3 == 0) else Decimal("0.00"),
        )
        temp_audit_logger.log(rec)

    assert temp_audit_logger.count() == 15

    page1, total1 = temp_audit_logger.get_summary_rows(page=1, page_size=10)
    assert len(page1) == 10
    assert total1 == 15

    page2, total2 = temp_audit_logger.get_summary_rows(page=2, page_size=10)
    assert len(page2) == 5

    # Filter by action
    escaped_txns, total_esc = temp_audit_logger.get_summary_rows(filter_action="escalate_to_human", page=1, page_size=20)
    assert total_esc == 7
    assert len(escaped_txns) == 7

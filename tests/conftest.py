"""
Shared test fixtures for the Project Meridian test suite.

Fixtures follow the principle: create the minimal valid object for a given
failure code, with sensible defaults that don't trigger any guardrail rules.
Tests that want to trigger a specific rule explicitly set the relevant fields.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from policy_engine.engine import PolicyEngine
from schemas.decision import ModelDecision, RecoveryAction, SHAPFeature
from schemas.transaction import FailedTransaction, FailureCode, PaymentMethod

# ── Helpers ────────────────────────────────────────────────────────────────────

def make_txn(
    failure_code: FailureCode = FailureCode.INSUFFICIENT_FUNDS,
    retry_count: int = 0,
    contact_count_24h: int = 0,
    last_contact_minutes_ago: int | None = None,
    hour_of_day: int = 14,  # 2pm — safely inside contact window
    amount: Decimal = Decimal("2500.00"),
    is_subscription: bool = False,
) -> FailedTransaction:
    """
    Build a FailedTransaction with safe defaults.
    Override only the fields relevant to the rule under test.
    """
    base_time = datetime(2024, 8, 15, hour_of_day, 30, 0, tzinfo=UTC)
    last_contact: datetime | None = None
    if last_contact_minutes_ago is not None:
        last_contact = base_time - timedelta(minutes=last_contact_minutes_ago)

    return FailedTransaction(
        txn_id=f"TXN-{failure_code.value}-{retry_count}",
        amount_inr=amount,
        failure_code=failure_code,
        payment_method=PaymentMethod.CARD,
        retry_count_so_far=retry_count,
        customer_id="CUST-001",
        merchant_id="MERCH-001",
        time_of_failure=base_time,
        gateway_raw_error=f"BANK_DECLINED: {failure_code.value}",
        customer_contact_count_24h=contact_count_24h,
        last_contact_time=last_contact,
        is_subscription=is_subscription,
    )


def make_model_decision(
    txn: FailedTransaction,
    action: RecoveryAction = RecoveryAction.RETRY_NOW,
    confidence: float = 0.75,
) -> ModelDecision:
    """Build a minimal ModelDecision for testing."""
    return ModelDecision(
        txn_id=txn.txn_id,
        recommended_action=action,
        confidence=confidence,
        shap_top_features=[
            SHAPFeature(
                feature_name="failure_code_category",
                shap_value=0.4,
                feature_value=txn.failure_code.value,
                direction="positive",
            )
        ],
    )


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
def engine() -> PolicyEngine:
    return PolicyEngine()


@pytest.fixture
def safe_txn() -> FailedTransaction:
    """A transaction that should pass through all guardrails unchanged."""
    return make_txn(
        failure_code=FailureCode.INSUFFICIENT_FUNDS,
        retry_count=0,
        contact_count_24h=0,
        last_contact_minutes_ago=None,
        hour_of_day=14,
    )

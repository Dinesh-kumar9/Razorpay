"""
Transaction schemas — the ingestion payload contract.

Every failed transaction entering the pipeline must validate against FailedTransaction.
If it doesn't, it is rejected at the ingestion boundary and never reaches the policy engine.
This is the first line of defence against garbage-in / garbage-out.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field, field_validator


class FailureCode(str, Enum):
    """
    Exhaustive enumeration of failure codes the policy engine reasons over.

    Distribution in our synthetic batch (per data_provenance.md):
      Soft decline  50%  — machine-retryable with right timing/method
      Hard risk     30%  — ALWAYS escalate_to_human; model is not consulted
      Card issue    15%  — instrument-level fix needed; nudge > retry
      System error   5%  — transient; retry_now is often optimal
    """

    # ── Soft declines (50%) ────────────────────────────────────────────────────
    INSUFFICIENT_FUNDS = "insufficient_funds"
    DO_NOT_HONOR = "do_not_honor"
    TRANSACTION_NOT_PERMITTED = "transaction_not_permitted"

    # ── Hard risk flags (30%) — hard-stop; NEVER auto-retry ───────────────────
    CARD_BLOCKED = "card_blocked"
    FRAUD_FLAG = "fraud_flag"
    KYC_HOLD = "kyc_hold"
    STOLEN_CARD = "stolen_card"

    # ── Card issues (15%) ──────────────────────────────────────────────────────
    CARD_EXPIRED = "card_expired"
    INVALID_CARD = "invalid_card"
    CARD_LIMIT_EXCEEDED = "card_limit_exceeded"

    # ── System / gateway errors (5%) ───────────────────────────────────────────
    NETWORK_TIMEOUT = "network_timeout"
    GATEWAY_ERROR = "gateway_error"
    BANK_UNAVAILABLE = "bank_unavailable"


# Failure codes that are unconditionally hard-stopped — exported for use in
# policy_engine/rules.py so the set is defined exactly once.
HARD_STOP_CODES: frozenset[FailureCode] = frozenset(
    {
        FailureCode.CARD_BLOCKED,
        FailureCode.FRAUD_FLAG,
        FailureCode.KYC_HOLD,
        FailureCode.STOLEN_CARD,
    }
)

# Failure codes where retry is physically impossible (instrument is invalid).
NO_RETRY_CODES: frozenset[FailureCode] = frozenset(
    {
        FailureCode.CARD_EXPIRED,
        FailureCode.INVALID_CARD,
    }
)


class PaymentMethod(str, Enum):
    """Payment instruments on the Razorpay platform."""

    UPI = "upi"
    CARD = "card"
    NETBANKING = "netbanking"
    WALLET = "wallet"
    EMI = "emi"


class FailedTransaction(BaseModel):
    """
    A single failed payment event entering the recovery pipeline.

    This is the canonical input type for all downstream stages (risk model,
    policy engine, LLM layer).  No stage may widen this schema; narrowing
    (using a subset of fields) is fine.
    """

    txn_id: str = Field(description="Unique transaction identifier")
    amount_inr: Decimal = Field(gt=Decimal("0"), description="Transaction amount in INR")
    failure_code: FailureCode
    payment_method: PaymentMethod
    retry_count_so_far: int = Field(ge=0, description="Number of retry attempts already made")
    customer_id: str
    merchant_id: str
    time_of_failure: datetime
    gateway_raw_error: str = Field(
        description="Raw error string from the payment gateway — unstructured, fed to LLM layer"
    )
    customer_contact_count_24h: int = Field(
        ge=0,
        description="Number of recovery-related contacts sent to this customer in the last 24 hours",
    )
    last_contact_time: datetime | None = Field(
        default=None,
        description="Timestamp of the most recent contact attempt; None if no prior contact",
    )
    is_subscription: bool = Field(
        default=False,
        description="True for recurring/subscription payments — affects retry urgency",
    )
    customer_opted_out: bool = Field(
        default=False,
        description=(
            "True if the customer has explicitly revoked consent for automated recovery contact. "
            "When True, OPT_OUT_001 fires immediately and stops all automated recovery — "
            "consent revocation is a hard stop per DPDP Act 2023 Chapter III."
        ),
    )
    recovery_cost_inr: Decimal = Field(
        default=Decimal("0"),
        ge=Decimal("0"),
        description=(
            "Cumulative gateway/processing cost already incurred for this recovery attempt, in INR. "
            "When this exceeds COST_THRESHOLD_PCT of amount_inr, COST_001 fires and stops "
            "further automated recovery to prevent value-destructive retries."
        ),
    )

    @field_validator("recovery_cost_inr")
    @classmethod
    def recovery_cost_non_negative(cls, v: Decimal) -> Decimal:
        if v < Decimal("0"):
            raise ValueError("recovery_cost_inr must be >= 0")
        return v

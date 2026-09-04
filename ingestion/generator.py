"""
Synthetic transaction generator — the data ingestion layer.

All 5,000 simulation transactions are generated here. No real customer or
merchant data is used at any point. The dataset is fully reproducible at
the default random_seed=42.

Full distribution rationale and sources: docs/data_provenance.md
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from schemas.transaction import FailedTransaction, FailureCode, PaymentMethod

# ── Failure code distribution (per data_provenance.md) ────────────────────────
# Weights must sum to 1.0
FAILURE_CODE_DISTRIBUTION: list[tuple[FailureCode, float]] = [
    # ── Soft declines (50%) ────────────────────────────────────────────────────
    (FailureCode.INSUFFICIENT_FUNDS, 0.25),
    (FailureCode.DO_NOT_HONOR, 0.15),
    (FailureCode.TRANSACTION_NOT_PERMITTED, 0.10),
    # ── Hard risk flags (30%) ──────────────────────────────────────────────────
    (FailureCode.CARD_BLOCKED, 0.12),
    (FailureCode.FRAUD_FLAG, 0.10),
    (FailureCode.KYC_HOLD, 0.05),
    (FailureCode.STOLEN_CARD, 0.03),
    # ── Card issues (15%) ──────────────────────────────────────────────────────
    (FailureCode.CARD_EXPIRED, 0.07),
    (FailureCode.INVALID_CARD, 0.05),
    (FailureCode.CARD_LIMIT_EXCEEDED, 0.03),
    # ── System / gateway errors (5%) ───────────────────────────────────────────
    (FailureCode.NETWORK_TIMEOUT, 0.02),
    (FailureCode.GATEWAY_ERROR, 0.02),
    (FailureCode.BANK_UNAVAILABLE, 0.01),
]

PAYMENT_METHOD_DISTRIBUTION: list[tuple[PaymentMethod, float]] = [
    (PaymentMethod.CARD, 0.45),
    (PaymentMethod.UPI, 0.30),
    (PaymentMethod.NETBANKING, 0.15),
    (PaymentMethod.WALLET, 0.07),
    (PaymentMethod.EMI, 0.03),
]

# Raw gateway error templates keyed by failure code
GATEWAY_ERROR_TEMPLATES: dict[FailureCode, list[str]] = {
    FailureCode.INSUFFICIENT_FUNDS: [
        "BANK_DECLINED: Insufficient funds in account",
        "ISSUER_DECLINED: Balance too low for transaction amount",
        "DECLINE_51: Insufficient funds",
    ],
    FailureCode.DO_NOT_HONOR: [
        "BANK_DECLINED: Do not honour - bank has blocked transaction",
        "ISSUER_DECLINED: Transaction not permitted by issuing bank",
        "DECLINE_05: Do not honour",
    ],
    FailureCode.TRANSACTION_NOT_PERMITTED: [
        "GATEWAY_ERR: Transaction type not permitted for this card",
        "ISSUER_DECLINED: Online transactions not enabled",
        "DECLINE_57: Transaction not permitted to cardholder",
    ],
    FailureCode.CARD_BLOCKED: [
        "BANK_DECLINED: Card blocked by issuing bank",
        "ISSUER_DECLINED: Card is blocked - contact bank",
        "DECLINE_62: Restricted card",
    ],
    FailureCode.FRAUD_FLAG: [
        "RISK_ENGINE: Transaction flagged as high-risk",
        "FRAUD_FILTER: Card flagged for suspicious activity",
        "DECLINE_59: Suspected fraud",
    ],
    FailureCode.KYC_HOLD: [
        "ISSUER_DECLINED: KYC documentation pending",
        "BANK_HOLD: Account under KYC review - transactions suspended",
        "COMPLIANCE_HOLD: KYC verification required",
    ],
    FailureCode.STOLEN_CARD: [
        "BANK_DECLINED: Card reported stolen",
        "ISSUER_DECLINED: Card status: stolen - retain if possible",
        "DECLINE_43: Stolen card",
    ],
    FailureCode.CARD_EXPIRED: [
        "GATEWAY_ERR: Card expiry date has passed",
        "ISSUER_DECLINED: Card expired",
        "DECLINE_54: Expired card",
    ],
    FailureCode.INVALID_CARD: [
        "GATEWAY_ERR: Card number failed Luhn check",
        "ISSUER_DECLINED: Invalid card number",
        "DECLINE_14: Invalid card number",
    ],
    FailureCode.CARD_LIMIT_EXCEEDED: [
        "BANK_DECLINED: Credit limit exceeded",
        "ISSUER_DECLINED: Transaction exceeds card spending limit",
        "DECLINE_65: Withdrawal limit exceeded",
    ],
    FailureCode.NETWORK_TIMEOUT: [
        "GATEWAY_TIMEOUT: No response from bank within 30s",
        "NETWORK_ERR: Connection to issuer timed out",
        "TIMEOUT: Bank connectivity issue - please retry",
    ],
    FailureCode.GATEWAY_ERROR: [
        "GATEWAY_ERR: Internal processing error - ref #GW5523",
        "PROCESSOR_ERR: Downstream service unavailable",
        "INTERNAL_ERR: Gateway processing failed",
    ],
    FailureCode.BANK_UNAVAILABLE: [
        "BANK_DOWN: Issuer bank is currently unavailable",
        "NETWORK_ERR: Unable to reach issuing bank",
        "SERVICE_ERR: Bank maintenance window",
    ],
}


def _weighted_choice(
    distribution: list[tuple[Any, float]], rng: random.Random
) -> Any:
    """Pick a value from a weighted distribution using the given RNG."""
    choices, weights = zip(*distribution, strict=False)
    return rng.choices(list(choices), weights=list(weights), k=1)[0]


def generate_transactions(
    n: int = 5000,
    random_seed: int = 42,
) -> list[FailedTransaction]:
    """
    Generate n synthetic failed transactions using the documented failure-code
    distribution. Every field is generated from realistic ranges:

    - amount_inr: log-uniform between Rs.100 and Rs.50,000 (covers small UPI to large card)
    - retry_count_so_far: 0-2 for most, rarely 3 (so guardrail edge cases appear naturally)
    - customer_contact_count_24h: 0 or 1 for most customers
    - time_of_failure: uniformly distributed over the last 7 days, covering all hours
    - last_contact_time: set for ~40% of transactions (those with prior contact)
    - customer_opted_out: ~2.5% of customers have revoked automated recovery consent
    - recovery_cost_inr: 2% of amount per prior retry attempt (simulates gateway fees)
      retry_count=1 -> 2% (below 5% COST_001 threshold)
      retry_count=2 -> 4% (below 5% COST_001 threshold)
      retry_count=3 -> 6% (ABOVE 5% COST_001 threshold -> COST_001 fires)

    The main rng stream (random_seed) is unchanged from the pre-field-addition version.
    The two new fields use a separate secondary_rng (seeded random_seed + 1000) so the
    existing transaction data (failure codes, amounts, timestamps) is not perturbed.

    The output is deterministic for a given random_seed.
    Default seed=42 produces the batch metrics reported in README.md (post v2 update).
    """
    rng = random.Random(random_seed)
    # Secondary RNG exclusively for new fields — does NOT affect the main rng stream
    secondary_rng = random.Random(random_seed + 1000)
    transactions: list[FailedTransaction] = []

    # Simulate over a 7-day window ending now
    window_end = datetime(2024, 8, 15, 23, 59, 59, tzinfo=UTC)
    window_start = window_end - timedelta(days=7)

    for i in range(n):
        failure_code: FailureCode = _weighted_choice(FAILURE_CODE_DISTRIBUTION, rng)
        payment_method: PaymentMethod = _weighted_choice(PAYMENT_METHOD_DISTRIBUTION, rng)

        # Amount: log-uniform between ₹100 and ₹50,000
        log_amount = rng.uniform(2.0, 4.7)  # log10(100) to log10(50000)
        amount = Decimal(str(round(10 ** log_amount, 2)))

        # Time of failure
        seconds_offset = rng.randint(0, int((window_end - window_start).total_seconds()))
        time_of_failure = window_start + timedelta(seconds=seconds_offset)

        # Retry history: most transactions are on their first or second attempt
        retry_count = rng.choices([0, 1, 2, 3], weights=[0.55, 0.25, 0.15, 0.05], k=1)[0]

        # Contact history: ~40% of customers have been contacted before
        has_prior_contact = rng.random() < 0.40
        contact_count_24h = 1 if has_prior_contact else 0
        last_contact_time: datetime | None = None
        if has_prior_contact:
            # Contact was 10–120 minutes before the failure time
            contact_offset = rng.randint(10, 120)
            last_contact_time = time_of_failure - timedelta(minutes=contact_offset)

        # Gateway error string
        error_templates = GATEWAY_ERROR_TEMPLATES.get(failure_code, ["UNKNOWN_ERROR"])
        gateway_raw_error = rng.choice(error_templates)

        # ~15% of transactions are subscriptions
        is_subscription = rng.random() < 0.15

        # ── New fields (secondary_rng — does not touch the main rng stream) ────

        # ~2.5% of customers have explicitly revoked automated recovery consent
        # (simulates DPDP opt-out rate observed in customer preference data)
        customer_opted_out = secondary_rng.random() < 0.025

        # Cumulative gateway/processing cost from prior retry attempts.
        # Approximate 2% of transaction amount per prior retry (gateway processing fee).
        # At retry_count=3 (6% cumulative), this crosses the COST_001 threshold (5%),
        # making further automated retries value-destructive.
        if retry_count > 0:
            recovery_cost_inr = amount * Decimal("0.02") * retry_count
        else:
            recovery_cost_inr = Decimal("0")

        txn = FailedTransaction(
            txn_id=f"TXN-{i:05d}-{random_seed}",
            amount_inr=amount,
            failure_code=failure_code,
            payment_method=payment_method,
            retry_count_so_far=retry_count,
            customer_id=f"CUST-{rng.randint(1, 2000):04d}",
            merchant_id=f"MERCH-{rng.randint(1, 200):03d}",
            time_of_failure=time_of_failure,
            gateway_raw_error=gateway_raw_error,
            customer_contact_count_24h=contact_count_24h,
            last_contact_time=last_contact_time,
            is_subscription=is_subscription,
            customer_opted_out=customer_opted_out,
            recovery_cost_inr=recovery_cost_inr,
        )
        transactions.append(txn)

    return transactions

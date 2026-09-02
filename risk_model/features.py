"""
Feature engineering — transforms FailedTransaction into numeric feature vectors.

Each feature is documented with its derivation and why it's predictive.
The feature set is designed to be explainable via SHAP: every feature maps
to something a merchant ops analyst would recognise as meaningful.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC

from schemas.transaction import FailedTransaction, FailureCode, PaymentMethod

# ── Lookup tables ──────────────────────────────────────────────────────────────

FAILURE_CODE_CATEGORY: dict[FailureCode, int] = {
    # 0 = hard risk (worst — never retryable)
    FailureCode.CARD_BLOCKED: 0,
    FailureCode.FRAUD_FLAG: 0,
    FailureCode.KYC_HOLD: 0,
    FailureCode.STOLEN_CARD: 0,
    # 1 = card issue (instrument-level — nudge is best)
    FailureCode.CARD_EXPIRED: 1,
    FailureCode.INVALID_CARD: 1,
    FailureCode.CARD_LIMIT_EXCEEDED: 1,
    # 2 = soft decline (timing-sensitive — delayed retry or nudge)
    FailureCode.INSUFFICIENT_FUNDS: 2,
    FailureCode.DO_NOT_HONOR: 2,
    FailureCode.TRANSACTION_NOT_PERMITTED: 2,
    # 3 = system error (transient — retry_now often works)
    FailureCode.NETWORK_TIMEOUT: 3,
    FailureCode.GATEWAY_ERROR: 3,
    FailureCode.BANK_UNAVAILABLE: 3,
}

PAYMENT_METHOD_RISK: dict[PaymentMethod, float] = {
    # Lower = more reliable instrument; higher = more prone to failure reoccurrence
    PaymentMethod.UPI: 0.10,
    PaymentMethod.WALLET: 0.12,
    PaymentMethod.NETBANKING: 0.18,
    PaymentMethod.CARD: 0.28,
    PaymentMethod.EMI: 0.35,
}

AMOUNT_TIERS: list[float] = [500.0, 2_000.0, 10_000.0, 50_000.0]
"""Breakpoints for amount bucketing: 0=<₹500, 1=₹500–2k, 2=₹2k–10k, 3=₹10k–50k, 4=>₹50k"""

# Feature names in the exact order they appear in the encoded vector.
# This ordering must never change after training — it would invalidate saved models.
FEATURE_NAMES: list[str] = [
    "failure_code_category",       # 0–3: hard_risk / card_issue / soft_decline / system_error
    "payment_method_risk",         # float [0.1, 0.35]
    "retry_attempt_number",        # 1-indexed retry count
    "amount_tier",                 # 0–4 bucket
    "is_outside_business_hours",   # 0/1
    "contact_proximity_score",     # [0, 1] normalized minutes since last contact
    "is_subscription",             # 0/1
    "hour_of_day",                 # 0–23
]


@dataclass
class FeatureVector:
    """
    Numeric feature representation of a FailedTransaction.

    All values are floats (XGBoost requirement). Boolean fields are 0.0/1.0.
    This dataclass is intermediate — call to_list() to get the model input.
    """

    failure_code_category: float
    payment_method_risk: float
    retry_attempt_number: float
    amount_tier: float
    is_outside_business_hours: float
    contact_proximity_score: float
    is_subscription: float
    hour_of_day: float

    def to_list(self) -> list[float]:
        """Return features in the canonical order defined by FEATURE_NAMES.

        Deliberately derived from FEATURE_NAMES rather than dataclass field order:
        a developer who reorders fields in the dataclass will not silently break the
        model, because the output order is governed by FEATURE_NAMES, not declaration order.
        """
        mapping: dict[str, float] = {
            "failure_code_category": self.failure_code_category,
            "payment_method_risk": self.payment_method_risk,
            "retry_attempt_number": self.retry_attempt_number,
            "amount_tier": self.amount_tier,
            "is_outside_business_hours": self.is_outside_business_hours,
            "contact_proximity_score": self.contact_proximity_score,
            "is_subscription": self.is_subscription,
            "hour_of_day": self.hour_of_day,
        }
        return [mapping[name] for name in FEATURE_NAMES]


def extract_features(txn: FailedTransaction) -> FeatureVector:
    """
    Extract numeric features from a FailedTransaction.

    All transformations are deterministic — the same transaction always produces
    the same feature vector. This is required for the batch simulation to be
    reproducible.
    """
    # contact_proximity_score: 1.0 = contacted just now, 0.0 = never contacted or > 2h ago
    contact_proximity = 0.0
    if txn.last_contact_time is not None:
        last = txn.last_contact_time
        failure = txn.time_of_failure
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        if failure.tzinfo is None:
            failure = failure.replace(tzinfo=UTC)
        minutes_since = (failure - last).total_seconds() / 60
        # Decay: 0 min → 1.0, 120 min → 0.0 (clamped)
        contact_proximity = max(0.0, 1.0 - (minutes_since / 120.0))

    hour = txn.time_of_failure.hour
    is_outside = float(hour < 8 or hour >= 21)

    return FeatureVector(
        failure_code_category=float(FAILURE_CODE_CATEGORY.get(txn.failure_code, 2)),
        payment_method_risk=PAYMENT_METHOD_RISK.get(txn.payment_method, 0.20),
        retry_attempt_number=float(txn.retry_count_so_far + 1),
        amount_tier=float(_bucket_amount(float(txn.amount_inr))),
        is_outside_business_hours=is_outside,
        contact_proximity_score=contact_proximity,
        is_subscription=float(txn.is_subscription),
        hour_of_day=float(hour),
    )


def _bucket_amount(amount: float) -> int:
    """Bucket transaction amount into 0–4 tiers."""
    for tier, threshold in enumerate(AMOUNT_TIERS):
        if amount < threshold:
            return tier
    return len(AMOUNT_TIERS)

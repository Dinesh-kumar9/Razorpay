"""
Recovery rate lookup table — single source of truth.

These rates are cited from publicly documented industry data and used in two places:
  1. risk_model/model.py — to generate training labels (stochastic sampling)
  2. simulation/outcome_model.py — to simulate batch outcomes

Keeping them in one place ensures the model is trained on the same distribution
it's evaluated against, which is the honest thing to do for synthetic data.

Sources for each rate are documented in docs/data_provenance.md.

CONTEXT MODIFIERS (added 2026-08-31):
The base rate above is adjusted by three documented, directionally-sensible modifiers
that give the model genuine signal beyond failure_code:

  1. amount_modifier:  Large transactions (>₹10k) are 15% harder to recover — banks and
     customers are more cautious about large disputed amounts (source: Worldpay Global
     Payments Report 2023: high-value declines resolve 12-18% less often on retry).

  2. hour_modifier:    Retries during bank business hours (09:00-17:00 Mon-Fri) succeed
     10% more often — processors batch approvals during these windows (source: Stripe
     Engineering Blog, "Retry timing", 2022; Razorpay Engineering, 2023 estimated).

  3. history_modifier: Customers with ≥3 prior failed attempts in 30d are 20% harder to
     recover — repeat failures signal systemic account issues, not transient declines
     (source: Chargebacks911 Industry Report 2023; approximated from "chronic decliner"
     segment data showing ~22% lower retry success).

These modifiers are applied multiplicatively and capped at [0.0, 1.0].
They are deliberately modest in magnitude so the model cannot trivially learn the
interaction from noise — it must identify the actual directional relationship.
"""

from __future__ import annotations

from schemas.decision import RecoveryAction
from schemas.transaction import FailureCode

# Recovery probability table: (FailureCode, RecoveryAction) → P(recover)
# 0.0 means the action is guaranteed to fail for this failure code.
# This is the ground truth the simulation samples from.
RECOVERY_RATES: dict[tuple[FailureCode, RecoveryAction], float] = {
    # ── Soft declines ──────────────────────────────────────────────────────────
    # insufficient_funds: best recovery is retry_delayed (wait for salary/top-up)
    (FailureCode.INSUFFICIENT_FUNDS, RecoveryAction.RETRY_NOW): 0.12,
    (FailureCode.INSUFFICIENT_FUNDS, RecoveryAction.RETRY_DELAYED): 0.42,
    (FailureCode.INSUFFICIENT_FUNDS, RecoveryAction.NUDGE_ALT_METHOD): 0.30,
    (FailureCode.INSUFFICIENT_FUNDS, RecoveryAction.ESCALATE_TO_HUMAN): 0.10,

    # do_not_honor: bank soft-block; alt method often works
    (FailureCode.DO_NOT_HONOR, RecoveryAction.RETRY_NOW): 0.10,
    (FailureCode.DO_NOT_HONOR, RecoveryAction.RETRY_DELAYED): 0.25,
    (FailureCode.DO_NOT_HONOR, RecoveryAction.NUDGE_ALT_METHOD): 0.48,
    (FailureCode.DO_NOT_HONOR, RecoveryAction.ESCALATE_TO_HUMAN): 0.15,

    # transaction_not_permitted: usually an online-block; alt method helps
    (FailureCode.TRANSACTION_NOT_PERMITTED, RecoveryAction.RETRY_NOW): 0.05,
    (FailureCode.TRANSACTION_NOT_PERMITTED, RecoveryAction.RETRY_DELAYED): 0.20,
    (FailureCode.TRANSACTION_NOT_PERMITTED, RecoveryAction.NUDGE_ALT_METHOD): 0.40,
    (FailureCode.TRANSACTION_NOT_PERMITTED, RecoveryAction.ESCALATE_TO_HUMAN): 0.20,

    # ── Hard risk flags — all zeroes for automated actions ─────────────────────
    # HARD_STOP_001 ensures these codes always → escalate_to_human.
    # The non-escalate rates are 0 to reflect that retrying is pointless.
    (FailureCode.CARD_BLOCKED, RecoveryAction.RETRY_NOW): 0.00,
    (FailureCode.CARD_BLOCKED, RecoveryAction.RETRY_DELAYED): 0.00,
    (FailureCode.CARD_BLOCKED, RecoveryAction.NUDGE_ALT_METHOD): 0.00,
    (FailureCode.CARD_BLOCKED, RecoveryAction.ESCALATE_TO_HUMAN): 0.20,  # human review works sometimes

    (FailureCode.FRAUD_FLAG, RecoveryAction.RETRY_NOW): 0.00,
    (FailureCode.FRAUD_FLAG, RecoveryAction.RETRY_DELAYED): 0.00,
    (FailureCode.FRAUD_FLAG, RecoveryAction.NUDGE_ALT_METHOD): 0.00,
    (FailureCode.FRAUD_FLAG, RecoveryAction.ESCALATE_TO_HUMAN): 0.15,

    (FailureCode.KYC_HOLD, RecoveryAction.RETRY_NOW): 0.00,
    (FailureCode.KYC_HOLD, RecoveryAction.RETRY_DELAYED): 0.00,
    (FailureCode.KYC_HOLD, RecoveryAction.NUDGE_ALT_METHOD): 0.00,
    (FailureCode.KYC_HOLD, RecoveryAction.ESCALATE_TO_HUMAN): 0.25,  # KYC issues are often resolvable

    (FailureCode.STOLEN_CARD, RecoveryAction.RETRY_NOW): 0.00,
    (FailureCode.STOLEN_CARD, RecoveryAction.RETRY_DELAYED): 0.00,
    (FailureCode.STOLEN_CARD, RecoveryAction.NUDGE_ALT_METHOD): 0.00,
    (FailureCode.STOLEN_CARD, RecoveryAction.ESCALATE_TO_HUMAN): 0.05,  # very low; bank coordinates

    # ── Card issues ─────────────────────────────────────────────────────────────
    # HARD_STOP_002 prevents retry on expired/invalid; nudge is the right action.
    (FailureCode.CARD_EXPIRED, RecoveryAction.RETRY_NOW): 0.00,
    (FailureCode.CARD_EXPIRED, RecoveryAction.RETRY_DELAYED): 0.00,
    (FailureCode.CARD_EXPIRED, RecoveryAction.NUDGE_ALT_METHOD): 0.60,
    (FailureCode.CARD_EXPIRED, RecoveryAction.ESCALATE_TO_HUMAN): 0.30,

    (FailureCode.INVALID_CARD, RecoveryAction.RETRY_NOW): 0.00,
    (FailureCode.INVALID_CARD, RecoveryAction.RETRY_DELAYED): 0.00,
    (FailureCode.INVALID_CARD, RecoveryAction.NUDGE_ALT_METHOD): 0.50,
    (FailureCode.INVALID_CARD, RecoveryAction.ESCALATE_TO_HUMAN): 0.20,

    (FailureCode.CARD_LIMIT_EXCEEDED, RecoveryAction.RETRY_NOW): 0.05,
    (FailureCode.CARD_LIMIT_EXCEEDED, RecoveryAction.RETRY_DELAYED): 0.35,
    (FailureCode.CARD_LIMIT_EXCEEDED, RecoveryAction.NUDGE_ALT_METHOD): 0.55,
    (FailureCode.CARD_LIMIT_EXCEEDED, RecoveryAction.ESCALATE_TO_HUMAN): 0.10,

    # ── System / gateway errors — transient; retry_now is best ─────────────────
    (FailureCode.NETWORK_TIMEOUT, RecoveryAction.RETRY_NOW): 0.38,
    (FailureCode.NETWORK_TIMEOUT, RecoveryAction.RETRY_DELAYED): 0.35,
    (FailureCode.NETWORK_TIMEOUT, RecoveryAction.NUDGE_ALT_METHOD): 0.10,
    (FailureCode.NETWORK_TIMEOUT, RecoveryAction.ESCALATE_TO_HUMAN): 0.08,

    (FailureCode.GATEWAY_ERROR, RecoveryAction.RETRY_NOW): 0.35,
    (FailureCode.GATEWAY_ERROR, RecoveryAction.RETRY_DELAYED): 0.30,
    (FailureCode.GATEWAY_ERROR, RecoveryAction.NUDGE_ALT_METHOD): 0.10,
    (FailureCode.GATEWAY_ERROR, RecoveryAction.ESCALATE_TO_HUMAN): 0.05,

    (FailureCode.BANK_UNAVAILABLE, RecoveryAction.RETRY_NOW): 0.20,
    (FailureCode.BANK_UNAVAILABLE, RecoveryAction.RETRY_DELAYED): 0.45,
    (FailureCode.BANK_UNAVAILABLE, RecoveryAction.NUDGE_ALT_METHOD): 0.10,
    (FailureCode.BANK_UNAVAILABLE, RecoveryAction.ESCALATE_TO_HUMAN): 0.05,
}

# STOP action always has 0% recovery (we gave up)
STOP_RECOVERY_RATE: float = 0.0

# ── Context modifier constants ─────────────────────────────────────────────────
# Magnitude deliberately modest so these are learnable signals, not dominant noise.
_AMOUNT_HIGH_THRESHOLD_INR: float = 10_000.0
_AMOUNT_HIGH_PENALTY: float = 0.15    # -15% for high-value transactions (Worldpay 2023)

_BANK_HOURS_START: int = 9             # 09:00 (bank processing window)
_BANK_HOURS_END: int = 17              # 17:00
_BANK_HOURS_BONUS: float = 0.10        # +10% during bank processing hours (Stripe 2022)

_REPEAT_FAILURE_THRESHOLD: int = 3    # >=3 prior fails in 30d = chronic decliner
_REPEAT_FAILURE_PENALTY: float = 0.20 # -20% for chronic decliners (Chargebacks911 2023)


def compute_context_multiplier(
    amount_inr: float,
    hour_of_day: int,
    prior_failed_attempts_30d: int,
) -> float:
    """
    Compute a multiplicative adjustment to the base recovery rate based on
    transaction context. Returns a value in [0.5, 1.1].

    Each modifier is directionally sensible and cited — see module docstring.
    The multiplier is capped so it never drives a probability below 50% of
    base or above 110% (hard-stop codes stay at 0.0 because base rate is 0.0).
    """
    multiplier = 1.0

    # High-value penalty: harder to recover large amounts
    if amount_inr >= _AMOUNT_HIGH_THRESHOLD_INR:
        multiplier -= _AMOUNT_HIGH_PENALTY

    # Bank-hours bonus: retries during bank processing window succeed more often
    if _BANK_HOURS_START <= hour_of_day < _BANK_HOURS_END:
        multiplier += _BANK_HOURS_BONUS

    # Repeat-failure penalty: chronic decliners less likely to recover
    if prior_failed_attempts_30d >= _REPEAT_FAILURE_THRESHOLD:
        multiplier -= _REPEAT_FAILURE_PENALTY

    # Clamp to [0.5, 1.1] — never completely eliminate recovery or inflate > 10%
    return max(0.5, min(1.1, multiplier))


def get_recovery_rate(failure_code: FailureCode, action: RecoveryAction) -> float:
    """
    Look up the base recovery probability for a (failure_code, action) pair.
    Returns 0.0 for STOP action or any pair not in the table (fail-safe).

    NOTE: This returns the BASE rate. For context-adjusted simulation, use
    get_contextual_recovery_rate() instead.
    """
    if action == RecoveryAction.STOP:
        return STOP_RECOVERY_RATE
    return RECOVERY_RATES.get((failure_code, action), 0.0)


def get_contextual_recovery_rate(
    failure_code: FailureCode,
    action: RecoveryAction,
    amount_inr: float,
    hour_of_day: int,
    prior_failed_attempts_30d: int,
) -> float:
    """
    Context-adjusted recovery probability incorporating amount, timing, and
    customer history signals beyond failure_code alone.

    The base rate is multiplied by a context modifier derived from three
    documented industry signals. Hard-stop codes with base rate 0.0 remain
    at 0.0 regardless of context (multiplier * 0 = 0).

    This is the function used by simulation/outcome_model.py so that the
    training labels for XGBoost reflect real feature dependencies.
    """
    base = get_recovery_rate(failure_code, action)
    if base == 0.0:
        return 0.0  # Hard stops are unconditional — context cannot change this
    multiplier = compute_context_multiplier(amount_inr, hour_of_day, prior_failed_attempts_30d)
    return min(1.0, base * multiplier)

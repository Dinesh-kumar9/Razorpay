"""
Outcome model — simulates recovery outcomes using documented, context-adjusted rates.

Recovery probability depends on (failure_code, action) as a base, PLUS three
context modifiers documented in risk_model/recovery_rates.py:
  - amount_inr: large transactions are harder to recover (Worldpay 2023)
  - hour_of_day: bank-hours window improves retry success (Stripe 2022)
  - prior_failed_attempts_30d: chronic decliners recover less often (Chargebacks911 2023)

This ensures the XGBoost model has genuine predictive signal beyond failure_code:
amount_tier, hour_of_day, and retry_attempt_number (proxy for history) each
shift the ground-truth label distribution, giving the model real features to learn.

Simulation methodology: Bernoulli sampling seeded with the batch RNG.
The same seed produces identical outcomes on re-run (reproducibility guarantee).
"""

from __future__ import annotations

import random
from decimal import Decimal

from risk_model.recovery_rates import get_contextual_recovery_rate
from schemas.audit import SimulatedOutcome
from schemas.decision import RecoveryAction
from schemas.transaction import FailedTransaction


def simulate_outcome(
    txn: FailedTransaction,
    final_action: RecoveryAction,
    rng: random.Random,
) -> SimulatedOutcome:
    """
    Sample a recovery outcome for a (transaction, action) pair.

    Uses the context-adjusted recovery rate (not just the base rate keyed
    on failure_code). This means the training signal and evaluation signal
    both respect amount, timing, and customer history — giving XGBoost
    genuine signal beyond failure_code.

    Args:
        txn: The failed transaction being evaluated.
        final_action: The action the policy engine decided on.
        rng: The seeded Random instance for the batch (ensures reproducibility).

    Returns:
        SimulatedOutcome with recovered flag and amount_recovered.
    """
    p_recover = get_contextual_recovery_rate(
        failure_code=txn.failure_code,
        action=final_action,
        amount_inr=float(txn.amount_inr),
        hour_of_day=txn.time_of_failure.hour,
        prior_failed_attempts_30d=txn.retry_count_so_far,
    )
    recovered = rng.random() < p_recover
    amount_recovered = txn.amount_inr if recovered else Decimal("0")

    return SimulatedOutcome(
        recovered=recovered,
        recovery_probability_used=p_recover,
        amount_recovered_inr=amount_recovered,
    )

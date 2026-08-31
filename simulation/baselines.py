"""
Baseline strategies for the batch simulation comparison.

Three baselines are evaluated against the agent on the same transaction set:

1. blind_retry_now: Retry every failed transaction ONCE, immediately, regardless of
   failure code. This is the absolute minimum naive strategy. Deliberately weak.
   Used as "vs single-attempt baseline" in reporting.

2. naive_multi_retry: Three attempts at fixed intervals (immediate, +24h, +72h).
   No nudge/escalate logic, no guardrails. Simulates what an unsophisticated
   merchant cron job does: keep retrying on a schedule. This is the realistic
   comparison point for the headline uplift metric.

3. never_retry: Do nothing. Recovery = Rs.0. This establishes the floor.

The agent's uplift is measured against BOTH baselines:
  - "vs single-attempt baseline" for secondary transparency
  - "vs realistic multi-retry baseline" as the headline pitch metric
"""

from __future__ import annotations

import random
from decimal import Decimal

from risk_model.recovery_rates import get_contextual_recovery_rate
from schemas.decision import RecoveryAction
from schemas.transaction import FailedTransaction

# Multi-retry attempt schedule: (action, delay) tuples in sequence
# Attempt 1: immediate RETRY_NOW
# Attempt 2: RETRY_DELAYED at +24h (next-day cron)
# Attempt 3: RETRY_DELAYED at +72h (3-day cron)
_MULTI_RETRY_SCHEDULE: list[tuple[RecoveryAction, float]] = [
    (RecoveryAction.RETRY_NOW, 0.0),
    (RecoveryAction.RETRY_DELAYED, 24.0),
    (RecoveryAction.RETRY_DELAYED, 72.0),
]


def run_blind_retry_baseline(
    transactions: list[FailedTransaction],
    rng: random.Random,
) -> Decimal:
    """
    Blind single-attempt retry: immediately retry every transaction ONCE,
    ignoring failure code.

    Expected behaviour: recovers ~5-7% of at-risk value because:
    - Hard risk flags (30%) have 0% recovery on retry_now
    - Soft declines (50%) recover better with delay, not immediate retry
    - System errors (5%) do recover on immediate retry
    Only card limit issues (subset of 15%) and system errors respond to immediate retry.

    Used as: secondary "vs single-attempt baseline" figure in reporting.
    """
    total_recovered = Decimal("0")
    for txn in transactions:
        p = get_contextual_recovery_rate(
            failure_code=txn.failure_code,
            action=RecoveryAction.RETRY_NOW,
            amount_inr=float(txn.amount_inr),
            hour_of_day=txn.time_of_failure.hour,
            prior_failed_attempts_30d=txn.retry_count_so_far,
        )
        if rng.random() < p:
            total_recovered += txn.amount_inr
    return total_recovered


def run_naive_multi_retry_baseline(
    transactions: list[FailedTransaction],
    rng: random.Random,
) -> Decimal:
    """
    Naive multi-retry: three attempts at fixed intervals — immediate, +24h, +72h.
    No nudge, no escalation, no guardrails. Simulates a merchant cron job.

    This is the REALISTIC comparison baseline for the headline uplift metric.
    An unsophisticated merchant running a retry cron job achieves this level.
    The agent must beat this to claim genuine value.

    Recovery logic: each attempt is independent. Transaction is considered
    recovered if ANY attempt succeeds (first success wins, stops retrying).
    """
    total_recovered = Decimal("0")
    for txn in transactions:
        for attempt_action, delay_hours in _MULTI_RETRY_SCHEDULE:
            # Simulate elapsed time: hour_of_day shifts forward by delay
            simulated_hour = (txn.time_of_failure.hour + int(delay_hours)) % 24
            # Prior attempts increment for the history modifier
            simulated_prior_attempts = txn.retry_count_so_far + int(delay_hours > 0)
            p = get_contextual_recovery_rate(
                failure_code=txn.failure_code,
                action=attempt_action,
                amount_inr=float(txn.amount_inr),
                hour_of_day=simulated_hour,
                prior_failed_attempts_30d=simulated_prior_attempts,
            )
            if rng.random() < p:
                total_recovered += txn.amount_inr
                break  # Transaction recovered — stop retrying
    return total_recovered


def run_never_retry_baseline(transactions: list[FailedTransaction]) -> Decimal:
    """
    Never retry: do nothing. Recovery = Rs.0. This is the floor.
    Included so judges can see the full range: never_retry → multi_retry → agent.
    """
    return Decimal("0")

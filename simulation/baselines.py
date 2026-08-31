"""
Baseline strategies for the batch simulation comparison.

Baselines evaluated against the agent on the same transaction set:

1. blind_retry_now: Retry every failed transaction ONCE, immediately, regardless of
   failure code. This is the absolute minimum naive strategy. Deliberately weak.

2. naive_multi_retry: Three attempts at fixed intervals (immediate, +24h, +72h).
   No nudge/escalate logic, no guardrails. Simulates what an unsophisticated
   merchant cron job does: keep retrying on a schedule.

3. naive_multi_retry_constrained: Same 3-attempt retry schedule, but gated by the
   identical rules the policy engine enforces (hard-stop codes skipped, contact caps
   respected, DND window respected, cooldown between attempts respected).

4. never_retry: Do nothing. Recovery = Rs.0. This establishes the floor.
"""

from __future__ import annotations

import random
from decimal import Decimal

from policy_engine.rules import (
    COOLDOWN_MINUTES,
    CONTACT_WINDOW_END_HOUR,
    CONTACT_WINDOW_START_HOUR,
    MAX_RETRIES_PER_TXN,
)
from risk_model.recovery_rates import get_contextual_recovery_rate
from schemas.decision import RecoveryAction
from schemas.transaction import HARD_STOP_CODES, NO_RETRY_CODES, FailedTransaction

# Multi-retry attempt schedule: (action, delay) tuples in sequence
# Attempt 1: immediate RETRY_NOW (0h)
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
    Naive multi-retry (unconstrained): three attempts at fixed intervals — immediate, +24h, +72h.
    No nudge, no escalation, no guardrails. Simulates an unconstrained merchant cron job.
    """
    recovered, _ = run_naive_multi_retry_with_violations(transactions, rng)
    return recovered


def run_naive_multi_retry_with_violations(
    transactions: list[FailedTransaction],
    rng: random.Random,
) -> tuple[Decimal, dict[str, int]]:
    """
    Run the unconstrained multi-retry baseline and tally rule violations
    for every action it takes against the policy engine's guardrail rules:
      - hard_stop_retry: retrying hard-stop / invalid card failure codes
      - contact_cap_exceeded: retries exceeding maximum retry limit (MAX_RETRIES_PER_TXN)
      - dnd_window_violation: attempts occurring outside the 08:00–21:00 window
      - cooldown_violation: immediate retry when last contact was within 30 minutes
    """
    total_recovered = Decimal("0")
    violations: dict[str, int] = {
        "hard_stop_retry": 0,
        "contact_cap_exceeded": 0,
        "dnd_window_violation": 0,
        "cooldown_violation": 0,
    }

    for txn in transactions:
        is_hard_stop = (txn.failure_code in HARD_STOP_CODES) or (txn.failure_code in NO_RETRY_CODES)

        for attempt_idx, (attempt_action, delay_hours) in enumerate(_MULTI_RETRY_SCHEDULE):
            simulated_hour = (txn.time_of_failure.hour + int(delay_hours)) % 24
            simulated_prior_attempts = txn.retry_count_so_far + attempt_idx

            # Tally violations for this unconstrained action
            if is_hard_stop:
                violations["hard_stop_retry"] += 1

            if simulated_prior_attempts >= MAX_RETRIES_PER_TXN:
                violations["contact_cap_exceeded"] += 1

            if simulated_hour < CONTACT_WINDOW_START_HOUR or simulated_hour >= CONTACT_WINDOW_END_HOUR:
                violations["dnd_window_violation"] += 1

            if delay_hours == 0.0 and txn.last_contact_time is not None:
                elapsed_min = (txn.time_of_failure - txn.last_contact_time).total_seconds() / 60.0
                if 0 <= elapsed_min < COOLDOWN_MINUTES:
                    violations["cooldown_violation"] += 1

            # Sample recovery outcome
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

    return total_recovered, violations


def run_naive_multi_retry_constrained(
    transactions: list[FailedTransaction],
    rng: random.Random,
) -> Decimal:
    """
    Naive multi-retry (constrained): same 3-attempt retry schedule (immediate, +24h, +72h),
    but gated by the identical rules the policy engine enforces:
      - hard-stop failure codes get ZERO retries
      - retry attempt count is capped at MAX_RETRIES_PER_TXN
      - DND contact window (08:00–21:00) is strictly respected
      - cooldown between consecutive attempts (30 min) is respected
    """
    total_recovered = Decimal("0")
    for txn in transactions:
        # Rule 1: Hard-stop codes get zero retries
        if (txn.failure_code in HARD_STOP_CODES) or (txn.failure_code in NO_RETRY_CODES):
            continue

        for attempt_idx, (attempt_action, delay_hours) in enumerate(_MULTI_RETRY_SCHEDULE):
            simulated_prior_attempts = txn.retry_count_so_far + attempt_idx

            # Rule 2: Max retries cap per transaction
            if simulated_prior_attempts >= MAX_RETRIES_PER_TXN:
                break

            # Rule 3: Cooldown on immediate retry
            if delay_hours == 0.0 and txn.last_contact_time is not None:
                elapsed_min = (txn.time_of_failure - txn.last_contact_time).total_seconds() / 60.0
                if 0 <= elapsed_min < COOLDOWN_MINUTES:
                    continue  # Cooldown active — cannot retry immediately

            # Rule 4: DND window (08:00 - 21:00)
            simulated_hour = (txn.time_of_failure.hour + int(delay_hours)) % 24
            if simulated_hour < CONTACT_WINDOW_START_HOUR or simulated_hour >= CONTACT_WINDOW_END_HOUR:
                continue  # Outside allowed window — cannot retry at this hour

            # Attempt is compliant — execute
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
    """Never retry: do nothing. Recovery = Rs.0. This is the floor."""
    return Decimal("0")

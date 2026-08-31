"""
Policy engine guardrail rules — the load-bearing component.

Each rule is a standalone function that takes a transaction and a proposed
action, and returns either None (rule did not fire) or a RuleResult (rule
fired — override with this action and this reason).

Design decisions documented in docs/adr/0002-policy-engine-overrides-model.md:
  - Rules are evaluated in priority order by PolicyEngine.evaluate().
  - The first rule that fires wins; subsequent rules are not evaluated.
  - Hard-stop rules are checked BEFORE the model is even consulted.
  - This file has ≥90% unit-test coverage (enforced by CI gate).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone

from schemas.decision import RecoveryAction
from schemas.transaction import HARD_STOP_CODES, NO_RETRY_CODES, FailedTransaction

# ── Constants (compliance parameters) ─────────────────────────────────────────

MAX_RETRIES_PER_TXN: int = 3
"""
Maximum retry attempts per transaction lifetime.
Beyond this, RATE_LIMIT_001 fires and returns STOP.
Rationale: banks flag repeated failed attempts as fraud signals after ~3–4 tries.
"""

MAX_CONTACTS_PER_CUSTOMER_24H: int = 1
"""
Maximum recovery-related contacts to a customer in any 24-hour window.
Rationale: DPDP Act 2023 — unsolicited commercial contact limits.
"""

COOLDOWN_MINUTES: int = 30
"""
Minimum minutes between consecutive retry attempts on the same transaction.
Rationale: Most payment gateways enforce a rate limit at the bank rails level;
submitting the same card within 30 minutes increases decline probability.
"""

CONTACT_WINDOW_START_HOUR: int = 8
CONTACT_WINDOW_END_HOUR: int = 21
"""
Customer contact is only permitted between 08:00–21:00 local time.
Rationale: TRAI DND regulations; contacting customers outside this window
constitutes unsolicited commercial communication.
"""

DEFAULT_RETRY_DELAY_MINUTES: int = 60
NEXT_WINDOW_DELAY_MINUTES: int = 480  # ~8 hours to next business window


# ── RuleResult ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RuleResult:
    """Returned by a rule function when the rule fires."""

    rule_id: str
    override_action: RecoveryAction
    reason: str
    retry_delay_minutes: int | None = None


# ── Hard-stop rules ────────────────────────────────────────────────────────────


def check_HARD_STOP_001(
    txn: FailedTransaction,
    proposed_action: RecoveryAction,  # noqa: ARG001 — model not consulted for hard stops
) -> RuleResult | None:
    """
    Hard stop: certain failure codes are unconditionally escalated to human review.

    The model is not consulted for these codes — the rule fires regardless of what
    the model would have recommended. This prevents the model from "recommending"
    a retry on a fraud-flagged or blocked card, which would (a) certainly fail and
    (b) potentially violate RBI fraud-prevention circulars.

    Codes: card_blocked, fraud_flag, kyc_hold, stolen_card.
    Compliance basis: RBI Master Direction on Fraud — FRMCs/PAs must escalate
    suspected fraud immediately; automated retries on flagged cards are prohibited.
    """
    if txn.failure_code in HARD_STOP_CODES:
        return RuleResult(
            rule_id="HARD_STOP_001",
            override_action=RecoveryAction.ESCALATE_TO_HUMAN,
            reason=(
                f"Failure code '{txn.failure_code.value}' is a hard-stop code "
                f"(fraud/block/KYC). Automated retry is prohibited. "
                f"Escalating to human review per RBI fraud-prevention guidelines."
            ),
        )
    return None


def check_HARD_STOP_002(
    txn: FailedTransaction,
    proposed_action: RecoveryAction,
) -> RuleResult | None:
    """
    Hard stop: expired or invalid cards cannot recover via retry.

    Retrying an expired card is guaranteed to fail — the failure is not transient,
    it's a permanent state of the instrument. The only recovery path is to nudge
    the customer to use a different payment method.

    This rule fires only when the model proposes a retry action, not when it
    correctly proposes nudge_alt_method or escalation.
    """
    retry_actions = {RecoveryAction.RETRY_NOW, RecoveryAction.RETRY_DELAYED}
    if txn.failure_code in NO_RETRY_CODES and proposed_action in retry_actions:
        return RuleResult(
            rule_id="HARD_STOP_002",
            override_action=RecoveryAction.NUDGE_ALT_METHOD,
            reason=(
                f"Failure code '{txn.failure_code.value}' is a permanent instrument failure. "
                f"Retrying the same instrument will always fail. "
                f"Overriding to nudge_alt_method to prompt the customer for a different card/method."
            ),
        )
    return None


# ── Rate-limit rules ───────────────────────────────────────────────────────────


def check_RATE_LIMIT_001(
    txn: FailedTransaction,
    proposed_action: RecoveryAction,
) -> RuleResult | None:
    """
    Rate limit: maximum retry attempts per transaction lifetime.

    Once a transaction has been retried MAX_RETRIES_PER_TXN times, no further
    automated retries are permitted. The action is set to STOP.

    Rationale: banks and payment networks flag repeated failed attempts as
    potential card testing or fraud. Exceeding ~3 retries increases the risk
    of a permanent instrument block, which harms the customer independently
    of the recovery goal.
    """
    retry_actions = {RecoveryAction.RETRY_NOW, RecoveryAction.RETRY_DELAYED}
    if txn.retry_count_so_far >= MAX_RETRIES_PER_TXN and proposed_action in retry_actions:
        return RuleResult(
            rule_id="RATE_LIMIT_001",
            override_action=RecoveryAction.STOP,
            reason=(
                f"Transaction has already been retried {txn.retry_count_so_far} times "
                f"(limit: {MAX_RETRIES_PER_TXN}). Further retries risk triggering "
                f"bank-side fraud flags. Stopping automated recovery."
            ),
        )
    return None


def check_RATE_LIMIT_002(
    txn: FailedTransaction,
    proposed_action: RecoveryAction,
) -> RuleResult | None:
    """
    Rate limit: maximum customer contacts per 24-hour window.

    If the customer has already been contacted MAX_CONTACTS_PER_CUSTOMER_24H
    times in the last 24 hours, a NUDGE action (which would generate another
    customer-facing message) is blocked and replaced with a silent retry delay.

    Rationale: DPDP Act 2023 prohibits excessive unsolicited commercial contact.
    Sending multiple nudge messages in a day is both non-compliant and harmful
    to customer experience.
    """
    if (
        proposed_action == RecoveryAction.NUDGE_ALT_METHOD
        and txn.customer_contact_count_24h >= MAX_CONTACTS_PER_CUSTOMER_24H
    ):
        return RuleResult(
            rule_id="RATE_LIMIT_002",
            override_action=RecoveryAction.RETRY_DELAYED,
            reason=(
                f"Customer '{txn.customer_id}' has already been contacted "
                f"{txn.customer_contact_count_24h} time(s) in the last 24 hours "
                f"(limit: {MAX_CONTACTS_PER_CUSTOMER_24H}). "
                f"Overriding nudge_alt_method to retry_delayed to avoid DPDP non-compliance."
            ),
            retry_delay_minutes=DEFAULT_RETRY_DELAY_MINUTES,
        )
    return None


# ── Cooldown rules ─────────────────────────────────────────────────────────────


def check_COOLDOWN_001(
    txn: FailedTransaction,
    proposed_action: RecoveryAction,
) -> RuleResult | None:
    """
    Cooldown: minimum time between consecutive retry attempts.

    If a retry_now is proposed but the customer was last contacted fewer than
    COOLDOWN_MINUTES ago, the action is downgraded to retry_delayed.

    Rationale: Submitting the same payment within a short window to the same
    bank/gateway often results in an immediate decline (gateway deduplication
    or bank rate-limiting). A minimum cooldown improves recovery probability
    and avoids unnecessary bank-side friction.

    Edge case: if last_contact_time is None, there is no prior contact, so
    the cooldown does not apply.
    """
    if proposed_action != RecoveryAction.RETRY_NOW:
        return None
    if txn.last_contact_time is None:
        return None

    # Ensure both datetimes are timezone-aware for comparison
    last_contact = txn.last_contact_time
    failure_time = txn.time_of_failure

    if last_contact.tzinfo is None:
        last_contact = last_contact.replace(tzinfo=timezone.utc)
    if failure_time.tzinfo is None:
        failure_time = failure_time.replace(tzinfo=timezone.utc)

    minutes_since_contact = (failure_time - last_contact).total_seconds() / 60

    if minutes_since_contact < COOLDOWN_MINUTES:
        return RuleResult(
            rule_id="COOLDOWN_001",
            override_action=RecoveryAction.RETRY_DELAYED,
            reason=(
                f"Last contact was {minutes_since_contact:.1f} minutes ago "
                f"(cooldown: {COOLDOWN_MINUTES} min). Immediate retry risks gateway "
                f"deduplication rejection. Downgrading to retry_delayed."
            ),
            retry_delay_minutes=DEFAULT_RETRY_DELAY_MINUTES,
        )
    return None


# ── Time-window rules ──────────────────────────────────────────────────────────


def check_WINDOW_001(
    txn: FailedTransaction,
    proposed_action: RecoveryAction,
) -> RuleResult | None:
    """
    Time window: no customer-facing contact outside 08:00–21:00 local time.

    A nudge_alt_method action sends a message to the customer. If the current
    time is outside the permitted contact window, the nudge is replaced with a
    delayed retry scheduled for the next window opening.

    Rationale: TRAI DND regulations prohibit unsolicited commercial communication
    outside 9am–9pm. We use a conservative 8am–9pm window to provide buffer.

    Note: 'local time' here uses the time embedded in the transaction's
    time_of_failure timestamp. In production this would use the customer's
    registered timezone; in simulation we treat it as IST.
    """
    if proposed_action != RecoveryAction.NUDGE_ALT_METHOD:
        return None

    failure_hour = txn.time_of_failure.hour
    if failure_hour < CONTACT_WINDOW_START_HOUR or failure_hour >= CONTACT_WINDOW_END_HOUR:
        return RuleResult(
            rule_id="WINDOW_001",
            override_action=RecoveryAction.RETRY_DELAYED,
            reason=(
                f"Current hour is {failure_hour:02d}:xx — outside the permitted contact "
                f"window ({CONTACT_WINDOW_START_HOUR:02d}:00–{CONTACT_WINDOW_END_HOUR:02d}:00). "
                f"TRAI DND rules prohibit nudge messages outside this window. "
                f"Scheduling retry for next window opening."
            ),
            retry_delay_minutes=NEXT_WINDOW_DELAY_MINUTES,
        )
    return None

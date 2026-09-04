"""
PolicyEngine — the guardrail layer with final authority over the model.

The engine evaluates rules in strict priority order. The first rule that fires
wins; remaining rules are not evaluated. This ensures predictable, auditable
behaviour: given the same transaction state and proposed action, the engine
always produces the same PolicyDecision.

Architecture decision: docs/adr/0002-policy-engine-overrides-model.md
"""

from __future__ import annotations

from collections.abc import Callable

from policy_engine.rules import (
    RuleResult,
    check_COOLDOWN_001,
    check_COST_001,
    check_HARD_STOP_001,
    check_HARD_STOP_002,
    check_OPT_OUT_001,
    check_RATE_LIMIT_001,
    check_RATE_LIMIT_002,
    check_WINDOW_001,
)
from schemas.decision import ModelDecision, PolicyDecision, RecoveryAction
from schemas.transaction import FailedTransaction

# Rule function signature type alias
RuleFn = Callable[[FailedTransaction, RecoveryAction], RuleResult | None]

# Priority-ordered list of rule functions.
# See ADR 0008 for the full priority-ordering rationale.
#
# HARD_STOP_001 is FIRST — RBI fraud/KYC/stolen-card escalation is a statutory
# obligation that supersedes DPDP consent revocation. ESCALATE_TO_HUMAN is an
# internal compliance routing action (not automated customer contact), so DPDP
# Chapter III consent revocation does not apply.
#
# OPT_OUT_001 is SECOND — consent revocation halts all automated recovery contact
# (retries, nudges) once fraud hard-stops have been handled.
#
# COST_001 is THIRD — economic stopping only applies if consent is valid.
# Remaining rules apply in compliance-priority order.
RULE_PRIORITY: list[RuleFn] = [
    check_HARD_STOP_001,   # RBI fraud/KYC mandatory escalation — supersedes consent
    check_OPT_OUT_001,     # DPDP consent revocation — stops automated recovery contact
    check_COST_001,        # Economic stop — value-destructive retry prevention
    check_HARD_STOP_002,
    check_RATE_LIMIT_001,
    check_RATE_LIMIT_002,
    check_COOLDOWN_001,
    check_WINDOW_001,
]


class PolicyEngine:
    """
    Deterministic guardrail engine with final authority over recovery actions.

    The engine is stateless — it takes a transaction and a model decision
    and returns a policy decision. State (retry counts, contact counts) is
    embedded in the FailedTransaction, not tracked here.

    Unit-test coverage ≥90% is enforced by CI (see pyproject.toml).
    The tests for this class are the most important tests in the repo.
    """

    def evaluate(
        self,
        txn: FailedTransaction,
        model_decision: ModelDecision,
    ) -> PolicyDecision:
        """
        Apply all guardrail rules in priority order and return the final decision.

        If any rule fires, the engine substitutes the compliant action and logs the
        override with the rule ID and plain-English reason. The model's original
        recommendation is always recorded in the PolicyDecision for transparency.

        If no rules fire, the model's recommendation passes through unchanged.
        """
        proposed = model_decision.recommended_action

        for rule_fn in RULE_PRIORITY:
            result = rule_fn(txn, proposed)
            if result is not None:
                # Rule fired — check if it actually changed the action
                actually_overridden = result.override_action != proposed
                return PolicyDecision(
                    txn_id=txn.txn_id,
                    final_action=result.override_action,
                    model_action=proposed,
                    was_overridden=actually_overridden,
                    rule_mandated=True,
                    override_reason=result.reason,
                    guardrail_rule_id=result.rule_id,
                    retry_delay_minutes=result.retry_delay_minutes,
                )

        # No rules fired — accept the model's recommendation verbatim.
        return PolicyDecision(
            txn_id=txn.txn_id,
            final_action=proposed,
            model_action=proposed,
            was_overridden=False,
            rule_mandated=False,
            override_reason=None,
            guardrail_rule_id=None,
            retry_delay_minutes=model_decision.retry_delay_minutes,
        )

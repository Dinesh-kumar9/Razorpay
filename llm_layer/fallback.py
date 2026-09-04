"""
Deterministic fallback explanations — guaranteed to produce valid LLMExplanation.

These templates fire when:
  (a) the GEMINI_API_KEY is not set
  (b) the API call raises any exception (timeout, rate limit, etc.)
  (c) Gemini's response fails JSON parsing
  (d) The parsed JSON fails Pydantic schema validation

The fallback is not a degraded experience — it's a designed safety net.
The `source="template"` field in the output makes it auditable.

Architecture decision: docs/adr/0005-llm-fallback-design.md
"""

from __future__ import annotations

from schemas.decision import PolicyDecision, RecoveryAction, SHAPFeature
from schemas.explanation import LLMExplanation

# Action-specific rationale templates.
# {top_feature} and {failure_code} are filled in from the available context.
_RATIONALE_TEMPLATES: dict[RecoveryAction, str] = {
    RecoveryAction.RETRY_NOW: (
        "This payment failed due to {failure_code}, which is typically a transient issue. "
        "The strongest signal ({top_feature}) suggests immediate retry has the highest "
        "chance of recovery for this transaction type."
    ),
    RecoveryAction.RETRY_DELAYED: (
        "This payment failed due to {failure_code}. Retrying immediately is unlikely to succeed — "
        "the signal {top_feature} indicates the issue requires time to resolve. "
        "The payment will be automatically retried after a waiting period."
    ),
    RecoveryAction.NUDGE_ALT_METHOD: (
        "This payment failed due to {failure_code}, which is an instrument-level issue that "
        "cannot be resolved by retrying the same method. The signal {top_feature} confirms "
        "the customer is likely to complete payment via a different method if prompted."
    ),
    RecoveryAction.ESCALATE_TO_HUMAN: (
        "This payment has been flagged due to {failure_code}, which requires human review. "
        "Automated recovery is not appropriate here. The signal {top_feature} indicates "
        "this case needs a trained agent to assess and resolve."
    ),
    RecoveryAction.STOP: (
        "All automated recovery attempts for this payment have been exhausted. "
        "The failure code {failure_code} and signal {top_feature} indicate further "
        "automated action would not improve the recovery probability."
    ),
}

_CAVEAT_TEMPLATES: dict[RecoveryAction, str] = {
    RecoveryAction.RETRY_NOW: (
        "Recovery is not guaranteed. If this retry fails, the system will escalate or switch strategy."
    ),
    RecoveryAction.RETRY_DELAYED: (
        "If the customer's situation doesn't change before the retry window, "
        "recovery via this method will still fail."
    ),
    RecoveryAction.NUDGE_ALT_METHOD: (
        "Recovery depends on the customer responding to the nudge. "
        "If there is no response, no further automated action will be taken."
    ),
    RecoveryAction.ESCALATE_TO_HUMAN: (
        "Human review takes time and may not result in recovery. "
        "The outcome depends on the agent's assessment."
    ),
    RecoveryAction.STOP: (
        "No further automated recovery will be attempted. "
        "Manual intervention is still possible through your merchant dashboard."
    ),
}

_FALLBACK_TEMPLATES: dict[RecoveryAction, str] = {
    RecoveryAction.RETRY_NOW: (
        "If the retry fails, the system will try retry_delayed or escalate based on updated signals."
    ),
    RecoveryAction.RETRY_DELAYED: (
        "If the delayed retry also fails, the system will nudge the customer to use an alternative method."
    ),
    RecoveryAction.NUDGE_ALT_METHOD: (
        "If the customer does not respond, the transaction will be marked as unrecovered."
    ),
    RecoveryAction.ESCALATE_TO_HUMAN: (
        "If human review does not resolve the issue, the transaction will be marked as unrecovered."
    ),
    RecoveryAction.STOP: (
        "No automated fallback. Contact the customer directly if you wish to pursue recovery."
    ),
}

_OVERRIDE_RATIONALE_PREFIX: dict[str, str] = {
    "HARD_STOP_001": (
        "The system's risk model suggested a different action, but a mandatory compliance rule "
        "overrode it: this failure code requires human review under RBI fraud-prevention guidelines. "
    ),
    "HARD_STOP_002": (
        "The risk model suggested retrying, but the payment instrument is permanently invalid. "
        "Retrying would always fail, so the system redirected to an alternative method request instead. "
    ),
    "RATE_LIMIT_001": (
        "The maximum number of automated retry attempts has been reached for this transaction. "
        "Further retries risk triggering bank-side fraud flags, so the system has stopped. "
    ),
    "RATE_LIMIT_002": (
        "The customer has already been contacted today. "
        "To comply with DPDP contact limits, the nudge was replaced with a silent retry. "
    ),
    "COOLDOWN_001": (
        "A retry was attempted too soon after the last contact. "
        "The system applied a cooldown delay to avoid gateway deduplication rejection. "
    ),
    "WINDOW_001": (
        "Customer contact is not permitted outside 8am–9pm. "
        "The nudge was rescheduled to the next permitted contact window. "
    ),
    "OPT_OUT_001": (
        "The customer has revoked consent for automated recovery contact. "
        "All automated recovery has been halted per DPDP Act 2023. "
    ),
    "COST_001": (
        "The cumulative gateway cost for this recovery attempt exceeds the economic threshold. "
        "Further automated retries would be value-destructive; recovery has been stopped. "
    ),
}

# Hinglish customer-facing SMS/WhatsApp message templates.
# Only populated for actions that involve outbound customer contact.
# STOP and ESCALATE_TO_HUMAN do not send customer messages — value is None.
_HINGLISH_CUSTOMER_MSG: dict[RecoveryAction, str | None] = {
    RecoveryAction.RETRY_NOW: (
        "Namaste! Aapka ₹{amount:.0f} ka payment fail ho gaya. "
        "Hum abhi dobara try kar rahe hain. "
        "Agar phir bhi fail ho toh kripya apna balance check karein."
    ),
    RecoveryAction.RETRY_DELAYED: (
        "Namaste! Aapka ₹{amount:.0f} ka payment abhi process nahi hua. "
        "Hum thodi der baad automatically retry karenge. "
        "Koi action ki zaroorat nahi — hum aapko update karenge."
    ),
    RecoveryAction.NUDGE_ALT_METHOD: (
        "Namaste! Aapka ₹{amount:.0f} ka payment complete nahi hua. "
        "Kripya UPI, Net Banking ya doosra card try karein: {payment_link}"
    ),
    RecoveryAction.ESCALATE_TO_HUMAN: None,
    RecoveryAction.STOP: None,
}


def get_fallback_explanation(
    policy_decision: PolicyDecision,
    shap_features: list[SHAPFeature],
    failure_code: str,
    amount_inr: float = 0.0,
) -> LLMExplanation:
    """
    Build a deterministic LLMExplanation from templates.

    This is guaranteed to succeed. Called when the LLM path fails for any reason.
    The optional `amount_inr` parameter is used to fill the Hinglish message template.
    """
    action = policy_decision.final_action
    top_feature = shap_features[0].feature_name if shap_features else "payment history"

    # Build rationale — prepend override context if applicable
    base_rationale = _RATIONALE_TEMPLATES.get(action, "Recovery action selected based on transaction signals.").format(
        failure_code=failure_code.replace("_", " "),
        top_feature=top_feature.replace("_", " "),
    )
    if policy_decision.was_overridden and policy_decision.guardrail_rule_id:
        override_prefix = _OVERRIDE_RATIONALE_PREFIX.get(
            policy_decision.guardrail_rule_id,
            "A compliance rule overrode the model's recommendation. ",
        )
        rationale = override_prefix + base_rationale
    else:
        rationale = base_rationale

    # Truncate to schema limits
    rationale = rationale[:400]
    caveat = _CAVEAT_TEMPLATES.get(action, "Outcomes are based on historical patterns and not guaranteed.")[:200]
    fallback = _FALLBACK_TEMPLATES.get(action, "No further automated action will be taken.")[:200]

    # Build Hinglish customer message (None for STOP / ESCALATE_TO_HUMAN)
    hinglish_template = _HINGLISH_CUSTOMER_MSG.get(action)
    if hinglish_template is not None:
        try:
            hinglish_msg: str | None = hinglish_template.format(
                amount=amount_inr,
                payment_link="pay.razorpay.com/retry",
            )[:300]
        except (KeyError, ValueError):
            hinglish_msg = None
    else:
        hinglish_msg = None

    return LLMExplanation(
        rationale=rationale,
        confidence_caveat=caveat,
        fallback_if_wrong=fallback,
        source="template",
        customer_message_hinglish=hinglish_msg,
    )

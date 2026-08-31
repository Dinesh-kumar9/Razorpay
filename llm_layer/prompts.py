"""
LLM prompt construction for the explanation layer.

The system prompt enforces JSON-only output and embeds the schema definition
inline so Claude has no ambiguity about what to produce. Every field has a
character limit because an unconstrained LLM explanation would make the audit
log unreadable — brevity is a feature, not a constraint.

Architecture decision: docs/adr/0005-llm-fallback-design.md
"""

from __future__ import annotations

from schemas.decision import PolicyDecision, RecoveryAction, SHAPFeature

SYSTEM_PROMPT = """You are an explanation engine for a payment recovery system used by Indian merchants.

Your ONLY output must be a valid JSON object matching EXACTLY this schema:
{
  "rationale": "<string, max 400 chars: why this recovery action was chosen, in plain English for a merchant ops analyst>",
  "confidence_caveat": "<string, max 200 chars: one limitation or uncertainty the merchant should know>",
  "fallback_if_wrong": "<string, max 200 chars: what the system will do if this action fails to recover the payment>"
}

Rules:
- Output ONLY the JSON object. No markdown, no code blocks, no preamble, no explanation outside the JSON.
- Do not mention XGBoost, SHAP, or ML. Write for a non-technical merchant.
- Use INR (₹) for any amount references.
- The rationale must be specific to the failure code and action provided, not generic.
- If the action was overridden by a compliance rule, explain BOTH what the model wanted AND why the rule overrode it.
"""


def build_user_prompt(
    policy_decision: PolicyDecision,
    shap_features: list[SHAPFeature],
    raw_gateway_error: str,
    amount_inr: float,
    failure_code: str,
) -> str:
    """
    Build the user-turn prompt for Claude.

    Passes structured context in a readable format rather than raw JSON to
    get better natural-language explanations from the model.
    """
    action_description = _describe_action(policy_decision.final_action)
    shap_lines = "\n".join(
        f"  - {f.feature_name}: {f.feature_value} (influence: {'+' if f.direction == 'positive' else '-'}{abs(f.shap_value):.3f})"
        for f in shap_features
    )

    override_context = ""
    if policy_decision.was_overridden:
        override_context = f"""
IMPORTANT: The AI model originally recommended '{policy_decision.model_action.value}',
but a compliance rule overrode it:
  Rule: {policy_decision.guardrail_rule_id}
  Reason: {policy_decision.override_reason}
Your rationale must explain this override to the merchant in plain language.
"""

    return f"""Transaction Details:
  Amount: ₹{amount_inr:,.2f}
  Failure code: {failure_code}
  Gateway error: "{raw_gateway_error}"
  Final recovery action: {policy_decision.final_action.value}
  Action description: {action_description}

Top predictive signals (do NOT mention these are from a model — describe them as signals):
{shap_lines}
{override_context}
Generate the JSON explanation now."""


def _describe_action(action: RecoveryAction) -> str:
    """Human-readable description of each recovery action for the LLM context."""
    descriptions = {
        RecoveryAction.RETRY_NOW: "Immediately retry the payment with the same instrument",
        RecoveryAction.RETRY_DELAYED: "Schedule the payment retry for later (typically 24 hours)",
        RecoveryAction.NUDGE_ALT_METHOD: "Send the customer a request to complete payment via a different method",
        RecoveryAction.ESCALATE_TO_HUMAN: "Route this transaction to a human agent for manual review",
        RecoveryAction.STOP: "Stop all automated recovery attempts for this transaction",
    }
    return descriptions.get(action, action.value)

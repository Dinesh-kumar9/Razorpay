"""
Decision schemas — the contract between risk model, policy engine, and LLM layer.

The flow is:
  FailedTransaction → ModelDecision → PolicyDecision → (LLM sees PolicyDecision)

PolicyDecision is the final action the system takes. It may differ from
ModelDecision if a guardrail rule overrode the model's recommendation.
The override is always logged with a rule ID and a plain-English reason.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RecoveryAction(str, Enum):
    """
    The four recovery actions available to the agent, plus STOP.

    Design note: STOP is not a model output — it is a policy engine output
    triggered when all retry options are exhausted (RATE_LIMIT_001).
    The model only recommends the first four.
    """

    RETRY_NOW = "retry_now"
    RETRY_DELAYED = "retry_delayed"
    NUDGE_ALT_METHOD = "nudge_alt_method"
    ESCALATE_TO_HUMAN = "escalate_to_human"
    STOP = "stop"


# Actions the model is trained to recommend (STOP is policy-only).
MODEL_CANDIDATE_ACTIONS: tuple[RecoveryAction, ...] = (
    RecoveryAction.RETRY_NOW,
    RecoveryAction.RETRY_DELAYED,
    RecoveryAction.NUDGE_ALT_METHOD,
    RecoveryAction.ESCALATE_TO_HUMAN,
)


class SHAPFeature(BaseModel):
    """A single SHAP feature contribution — fed into the LLM explanation prompt."""

    feature_name: str
    shap_value: float = Field(
        description="Positive = pushes toward the recommended action; negative = away from it"
    )
    feature_value: str = Field(description="Human-readable display value, e.g. '42%' or 'card_blocked'")
    direction: Literal["positive", "negative"] = Field(description="Direction of SHAP influence on the recommended action")


class ModelDecision(BaseModel):
    """
    Output of the risk model for a single transaction.

    The recommended_action is a *proposal* — the policy engine has final authority.
    If no guardrail fires, this action becomes the final action verbatim.
    If a guardrail fires, this is logged for transparency and override-rate tracking.
    """

    model_config = ConfigDict(protected_namespaces=())

    txn_id: str
    recommended_action: RecoveryAction
    confidence: float = Field(ge=0.0, le=1.0, description="Model's P(recover) for the winning action")
    retry_delay_minutes: int | None = Field(
        default=None,
        description="Populated only when recommended_action == RETRY_DELAYED",
    )
    shap_top_features: list[SHAPFeature] = Field(
        description="Top-3 SHAP features for the winning action — always exactly 3 entries",
        min_length=1,
        max_length=3,
    )
    p_recover_by_action: dict[str, float] = Field(
        default_factory=dict,
        description="P(recover) for each candidate action — for audit transparency",
    )


class PolicyDecision(BaseModel):
    """
    Output of the policy/guardrail engine — the final, authoritative decision.

    This is the payload that flows to:
    - The LLM explanation layer (advisory input — cannot change this decision)
    - The execution layer
    - The audit log
    """

    model_config = ConfigDict(protected_namespaces=())

    txn_id: str
    final_action: RecoveryAction
    model_action: RecoveryAction = Field(description="What the model originally recommended")
    was_overridden: bool = Field(
        description="True if the policy engine substituted a different action than the model's recommendation (final_action != model_action)"
    )
    rule_mandated: bool = Field(
        default=False,
        description="True if a guardrail rule fired, regardless of whether it substituted a different action",
    )
    override_reason: str | None = Field(
        default=None,
        description="Plain-English compliance/business reason for the override — always set when was_overridden=True",
    )
    guardrail_rule_id: str | None = Field(
        default=None,
        description="The specific rule ID that fired, e.g. 'HARD_STOP_001' — always set when was_overridden=True",
    )
    retry_delay_minutes: int | None = Field(
        default=None,
        description="Delay in minutes when final_action == RETRY_DELAYED",
    )

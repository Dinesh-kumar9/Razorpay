"""
Test suite for the LLM explanation layer (Google Gemini 2.5 Flash).

Key invariants:
  1. explain() NEVER raises — any failure returns a valid LLMExplanation
  2. Fallback sets source="template"
  3. Successful LLM call sets source="llm"
  4. Fallback is always schema-valid
"""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from llm_layer.client import LLMExplainer
from llm_layer.fallback import get_fallback_explanation
from schemas.decision import PolicyDecision, RecoveryAction, SHAPFeature
from schemas.explanation import LLMExplanation


def make_policy_decision(action: RecoveryAction = RecoveryAction.RETRY_NOW) -> PolicyDecision:
    return PolicyDecision(
        txn_id="TXN-TEST",
        final_action=action,
        model_action=action,
        was_overridden=False,
    )


def make_shap_features() -> list[SHAPFeature]:
    return [
        SHAPFeature(
            feature_name="failure_code_category",
            shap_value=0.5,
            feature_value="soft_decline",
            direction="positive",
        )
    ]


class TestFallbackExplanation:
    """Fallback always produces valid LLMExplanation for every action."""

    @pytest.mark.parametrize(
        "action",
        [
            RecoveryAction.RETRY_NOW,
            RecoveryAction.RETRY_DELAYED,
            RecoveryAction.NUDGE_ALT_METHOD,
            RecoveryAction.ESCALATE_TO_HUMAN,
            RecoveryAction.STOP,
        ],
    )
    def test_fallback_always_returns_valid_explanation(self, action: RecoveryAction) -> None:
        pd = make_policy_decision(action)
        exp = get_fallback_explanation(pd, make_shap_features(), "insufficient_funds")
        assert isinstance(exp, LLMExplanation)
        assert exp.source == "template"
        assert len(exp.rationale) > 0
        assert len(exp.confidence_caveat) > 0
        assert len(exp.fallback_if_wrong) > 0

    def test_fallback_respects_field_length_limits(self) -> None:
        pd = make_policy_decision(RecoveryAction.RETRY_NOW)
        exp = get_fallback_explanation(pd, make_shap_features(), "card_blocked")
        assert len(exp.rationale) <= 400
        assert len(exp.confidence_caveat) <= 200
        assert len(exp.fallback_if_wrong) <= 200

    def test_fallback_includes_override_context_when_overridden(self) -> None:
        pd = PolicyDecision(
            txn_id="TXN-TEST",
            final_action=RecoveryAction.ESCALATE_TO_HUMAN,
            model_action=RecoveryAction.RETRY_NOW,
            was_overridden=True,
            override_reason="Card blocked: RBI fraud prevention guidelines.",
            guardrail_rule_id="HARD_STOP_001",
        )
        exp = get_fallback_explanation(pd, make_shap_features(), "card_blocked")
        assert exp.source == "template"
        assert len(exp.rationale) > 10

    def test_fallback_with_empty_shap_features(self) -> None:
        pd = make_policy_decision(RecoveryAction.RETRY_DELAYED)
        exp = get_fallback_explanation(pd, [], "insufficient_funds")
        assert isinstance(exp, LLMExplanation)
        assert exp.source == "template"


class TestLLMExplainer:
    """LLMExplainer.explain() never raises; falls back on any failure."""

    def test_no_api_key_returns_template(self) -> None:
        """When GEMINI_API_KEY is empty, template fallback is used immediately."""
        explainer = LLMExplainer(api_key="", groq_api_key="")
        pd = make_policy_decision()
        result = explainer.explain(pd, make_shap_features(), "bank error", Decimal("1000"), "insufficient_funds")
        assert isinstance(result, LLMExplanation)
        assert result.source == "template"

    def test_api_exception_returns_template(self) -> None:
        """Gemini raising any exception -> template fallback, no raise."""
        explainer = LLMExplainer(api_key="mock_key", groq_api_key="")
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = RuntimeError("API down")
        explainer._gemini_client = mock_client

        pd = make_policy_decision()
        result = explainer.explain(pd, make_shap_features(), "timeout", Decimal("2500"), "network_timeout")
        assert isinstance(result, LLMExplanation)
        assert result.source == "template"

    def test_malformed_json_returns_template(self) -> None:
        """Gemini returning non-JSON -> template fallback."""
        explainer = LLMExplainer(api_key="mock_key", groq_api_key="")
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = "Not JSON at all"
        mock_client.models.generate_content.return_value = mock_response
        explainer._gemini_client = mock_client

        pd = make_policy_decision()
        result = explainer.explain(pd, make_shap_features(), "error", Decimal("500"), "do_not_honor")
        assert isinstance(result, LLMExplanation)
        assert result.source == "template"

    def test_schema_validation_failure_returns_template(self) -> None:
        """Gemini returning JSON with missing fields -> Pydantic rejects -> template fallback."""
        explainer = LLMExplainer(api_key="mock_key", groq_api_key="")
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = '{"wrong_field": "wrong_value"}'
        mock_client.models.generate_content.return_value = mock_response
        explainer._gemini_client = mock_client

        pd = make_policy_decision()
        result = explainer.explain(pd, make_shap_features(), "error", Decimal("500"), "do_not_honor")
        assert isinstance(result, LLMExplanation)
        assert result.source == "template"

    def test_valid_gemini_response_returns_llm_source(self) -> None:
        """Valid Gemini SDK response → source='llm'."""
        explainer = LLMExplainer(api_key="mock_key", groq_api_key="")
        mock_client = MagicMock()
        mock_response = MagicMock()
        valid_json = json.dumps({
            "rationale": "This payment failed due to insufficient funds. Historical data shows delayed retry works best.",
            "confidence_caveat": "Recovery is not guaranteed if the customer balance does not increase.",
            "fallback_if_wrong": "If delayed retry fails, a nudge for alternative payment will be sent.",
        })
        mock_response.text = valid_json
        mock_client.models.generate_content.return_value = mock_response
        explainer._gemini_client = mock_client

        pd = make_policy_decision(RecoveryAction.RETRY_DELAYED)
        result = explainer.explain(pd, make_shap_features(), "insufficient funds", Decimal("3000"), "insufficient_funds")
        assert isinstance(result, LLMExplanation)
        assert result.source == "llm"
        assert "insufficient funds" in result.rationale.lower()

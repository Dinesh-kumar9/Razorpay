"""
Test suite for the LLM explanation layer.

Key invariants:
  1. explain() NEVER raises — any failure returns a valid LLMExplanation
  2. Fallback sets source="template"
  3. Successful LLM call sets source="llm"
  4. Fallback is always schema-valid
"""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

import anthropic
import pytest
import requests

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
        """When ANTHROPIC_API_KEY is not set, fallback is used immediately."""
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "", "ANTHROPIC_BASE_URL": ""}, clear=False):
            explainer = LLMExplainer()
        pd = make_policy_decision()
        result = explainer.explain(pd, make_shap_features(), "bank error", Decimal("1000"), "insufficient_funds")
        assert isinstance(result, LLMExplanation)
        assert result.source == "template"

    def test_api_exception_returns_template(self) -> None:
        """API call raising any exception → fallback, no raise."""
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-test", "ANTHROPIC_BASE_URL": ""}, clear=False):
            explainer = LLMExplainer()

        explainer._has_key = True
        explainer._is_openrouter = False
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = anthropic.APIError(
            message="Service unavailable", request=MagicMock(), body={}
        )
        explainer._client = mock_client

        pd = make_policy_decision()
        result = explainer.explain(pd, make_shap_features(), "timeout", Decimal("2500"), "network_timeout")
        assert isinstance(result, LLMExplanation)
        assert result.source == "template"

    def test_malformed_json_returns_template(self) -> None:
        """LLM returning non-JSON → fallback."""
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-test", "ANTHROPIC_BASE_URL": ""}, clear=False):
            explainer = LLMExplainer()

        explainer._has_key = True
        explainer._is_openrouter = False
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="Sorry, I cannot help with that.")]
        mock_client.messages.create.return_value = mock_response
        explainer._client = mock_client

        pd = make_policy_decision()
        result = explainer.explain(pd, make_shap_features(), "error", Decimal("500"), "do_not_honor")
        assert isinstance(result, LLMExplanation)
        assert result.source == "template"

    def test_schema_validation_failure_returns_template(self) -> None:
        """LLM returning JSON with wrong fields → Pydantic rejects → fallback."""
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-test", "ANTHROPIC_BASE_URL": ""}, clear=False):
            explainer = LLMExplainer()

        explainer._has_key = True
        explainer._is_openrouter = False
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text='{"wrong_field": "wrong_value"}')]
        mock_client.messages.create.return_value = mock_response
        explainer._client = mock_client

        pd = make_policy_decision()
        result = explainer.explain(pd, make_shap_features(), "error", Decimal("500"), "do_not_honor")
        assert isinstance(result, LLMExplanation)
        assert result.source == "template"

    def test_valid_anthropic_response_returns_llm_source(self) -> None:
        """Valid Claude Anthropic SDK response → source='llm'."""
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-test", "ANTHROPIC_BASE_URL": ""}, clear=False):
            explainer = LLMExplainer()

        explainer._has_key = True
        explainer._is_openrouter = False
        mock_client = MagicMock()
        mock_response = MagicMock()
        valid_json = json.dumps({
            "rationale": "This payment failed due to insufficient funds. Historical data shows delayed retry works best.",
            "confidence_caveat": "Recovery is not guaranteed if the customer's balance doesn't increase.",
            "fallback_if_wrong": "If delayed retry fails, a nudge for alternative payment will be sent.",
        })
        mock_response.content = [MagicMock(text=valid_json)]
        mock_client.messages.create.return_value = mock_response
        explainer._client = mock_client

        pd = make_policy_decision(RecoveryAction.RETRY_DELAYED)
        result = explainer.explain(pd, make_shap_features(), "insufficient funds", Decimal("3000"), "insufficient_funds")
        assert isinstance(result, LLMExplanation)
        assert result.source == "llm"
        assert "insufficient funds" in result.rationale.lower()

    def test_valid_openrouter_response_returns_llm_source(self) -> None:
        """Valid Claude OpenRouter response → source='llm'."""
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-or-v1-test", "ANTHROPIC_BASE_URL": "https://openrouter.ai/api/v1"}, clear=False):
            explainer = LLMExplainer()

        valid_json = json.dumps({
            "rationale": "This payment failed due to insufficient funds. Historical data shows delayed retry works best.",
            "confidence_caveat": "Recovery is not guaranteed if the customer's balance doesn't increase.",
            "fallback_if_wrong": "If delayed retry fails, a nudge for alternative payment will be sent.",
        })

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": f"```json\n{valid_json}\n```"}}]
        }

        with patch("requests.post", return_value=mock_resp):
            pd = make_policy_decision(RecoveryAction.RETRY_DELAYED)
            result = explainer.explain(pd, make_shap_features(), "insufficient funds", Decimal("3000"), "insufficient_funds")

        assert isinstance(result, LLMExplanation)
        assert result.source == "llm"
        assert "insufficient funds" in result.rationale.lower()

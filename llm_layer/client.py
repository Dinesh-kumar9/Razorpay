"""
LLM explanation client — Google Gemini 2.5 Flash, advisory-only, never blocks the pipeline.

This client has one contract: given a policy decision and context, return a
valid LLMExplanation. It NEVER raises an exception. On any failure (missing key,
API error, malformed response, schema validation failure), it returns the deterministic
template fallback and logs the reason.

Architecture decisions:
  - docs/adr/0001-llm-has-no-execution-authority.md
  - docs/adr/0004-no-agent-framework.md
  - docs/adr/0005-single-llm-provider-no-fallback.md
"""

from __future__ import annotations

import json
import logging
import os
from decimal import Decimal

from google import genai
from google.genai import types
from pydantic import ValidationError

from config import get_settings
from llm_layer.fallback import get_fallback_explanation
from llm_layer.prompts import SYSTEM_PROMPT, build_user_prompt
from schemas.decision import PolicyDecision, SHAPFeature
from schemas.explanation import LLMExplanation

logger = logging.getLogger(__name__)

# Gemini 2.5 Flash — fast, structured output support, free-tier eligible
GEMINI_MODEL = "gemini-2.5-flash"
MAX_TOKENS = 1000
TIMEOUT_SECONDS = 12.0


class LLMExplainer:
    """
    Advisory-only explanation layer backed by Google Gemini 2.5 Flash.

    The LLM has zero execution authority. It cannot change the final_action.
    It can only produce a natural-language explanation of a decision already made.

    Call explain() in a fire-and-fallback pattern:
        explanation = explainer.explain(...)  # always returns LLMExplanation
        # No exception handling needed — fallback is internal.
    """

    def __init__(self, api_key: str | None = None) -> None:
        settings = get_settings()
        if api_key is None:
            api_key = settings.gemini_api_key.strip()

        self._api_key = api_key
        self._has_key = bool(api_key)
        self._client: genai.Client | None = None

        if self._has_key:
            # Set GOOGLE_GENAI_USE_VERTEXAI in the environment so the SDK
            # uses Developer API (API-key mode) rather than defaulting to Vertex AI.
            # This is a google-genai SDK initialisation requirement, not a config read.
            os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = str(settings.google_genai_use_vertexai).lower()
            self._client = genai.Client(api_key=api_key)
            logger.info("LLM explainer initialized with Google Gemini 2.5 Flash via google-genai SDK.")
        else:
            logger.info(
                "GEMINI_API_KEY not set — LLM explainer will use template fallback for all decisions."
            )

    def explain(
        self,
        policy_decision: PolicyDecision,
        shap_features: list[SHAPFeature],
        raw_gateway_error: str,
        amount_inr: Decimal,
        failure_code: str,
    ) -> LLMExplanation:
        """
        Generate a natural-language explanation for the given policy decision.

        Attempt order:
          1. Call Gemini 2.5 Flash with native response_schema=LLMExplanation
          2. Parse JSON from response
          3. Validate against LLMExplanation schema
          4. If any step fails -> return deterministic template fallback

        This method NEVER raises. The pipeline never blocks on an LLM call.
        """
        if not self._has_key or self._client is None:
            logger.info("FALLBACK")
            return get_fallback_explanation(policy_decision, shap_features, failure_code)

        try:
            user_prompt = build_user_prompt(
                policy_decision=policy_decision,
                shap_features=shap_features,
                raw_gateway_error=raw_gateway_error,
                amount_inr=float(amount_inr),
                failure_code=failure_code,
            )

            config = types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=LLMExplanation,
                temperature=0.2,
                max_output_tokens=MAX_TOKENS,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            )

            response = self._client.models.generate_content(
                model=GEMINI_MODEL,
                contents=user_prompt,
                config=config,
            )

            raw_text = response.text.strip() if response.text else ""
            if not raw_text:
                logger.warning("Empty response received from Gemini; using template fallback.")
                logger.info("FALLBACK")
                return get_fallback_explanation(policy_decision, shap_features, failure_code)

            # Strip code fences if present
            if raw_text.startswith("```"):
                lines = raw_text.split("\n")
                raw_text = "\n".join(lines[1:-1]) if len(lines) > 2 else raw_text
                raw_text = raw_text.strip()
                if raw_text.startswith("json"):
                    raw_text = raw_text[4:].strip()

            parsed = json.loads(raw_text)
            if isinstance(parsed, dict):
                # Ensure length constraints
                if "rationale" in parsed and isinstance(parsed["rationale"], str):
                    parsed["rationale"] = parsed["rationale"][:590]
                if "confidence_caveat" in parsed and isinstance(parsed["confidence_caveat"], str):
                    parsed["confidence_caveat"] = parsed["confidence_caveat"][:340]
                if "fallback_if_wrong" in parsed and isinstance(parsed["fallback_if_wrong"], str):
                    parsed["fallback_if_wrong"] = parsed["fallback_if_wrong"][:340]

            explanation = LLMExplanation(**parsed, source="llm")
            logger.info("LIVE_CALL")
            return explanation

        except json.JSONDecodeError as exc:
            logger.warning("Gemini non-JSON response: %s", exc)
        except ValidationError as exc:
            logger.warning("Gemini schema validation failed: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Gemini API error [%s]: %s", type(exc).__name__, exc)

        logger.info("FALLBACK")
        return get_fallback_explanation(policy_decision, shap_features, failure_code)

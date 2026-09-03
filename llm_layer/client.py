"""
LLM explanation client -- single-provider, advisory-only, never blocks the pipeline.

Attempt order:
  1. Google Gemini (gemini-3.6-flash) via google-genai SDK
  2. Deterministic template fallback (llm_layer/fallback.py)

This client has one contract: given a policy decision and context, return a
valid LLMExplanation. It NEVER raises an exception. On any failure (missing key,
API error, quota exhausted, malformed response, schema validation failure) it
falls through to the deterministic template.

Architecture decisions:
  - docs/adr/0001-llm-has-no-execution-authority.md
  - docs/adr/0004-no-agent-framework.md
  - docs/adr/0005-llm-fallback-design.md  (single-provider invariant)
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

# -- Provider constants -------------------------------------------------------
# gemini-3.6-flash: current model for new API keys.
# API returns 404 for gemini-2.5-flash: "use models/gemini-3.6-flash for the latest feature"
GEMINI_MODEL: str = "gemini-3.6-flash"

MAX_TOKENS: int = 1000
TIMEOUT_SECONDS: float = 5.0  # fail fast -> template reached quickly


def _parse_and_validate(raw_text: str, source: str) -> LLMExplanation | None:
    """
    Parse JSON text into a validated LLMExplanation. Returns None on any error.
    """
    text = raw_text.strip()

    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1]) if len(lines) > 2 else text
        text = text.strip()
        if text.startswith("json"):
            text = text[4:].strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("%s non-JSON response: %s", source, exc)
        return None

    if not isinstance(parsed, dict):
        logger.warning("%s returned non-dict JSON", source)
        return None

    # Enforce field length constraints
    for field, limit in [
        ("rationale", 590),
        ("confidence_caveat", 340),
        ("fallback_if_wrong", 340),
    ]:
        if field in parsed and isinstance(parsed[field], str):
            parsed[field] = parsed[field][:limit]

    try:
        return LLMExplanation(**parsed, source="llm")
    except (ValidationError, TypeError) as exc:
        logger.warning("%s schema validation failed: %s", source, exc)
        return None


class LLMExplainer:
    """
    Advisory-only explanation layer. Single-provider design per ADR 0005.

    Provider order: Gemini 2.5 Flash -> deterministic template fallback

    The LLM has zero execution authority. It cannot change the final_action.
    Call explain() -- it always returns LLMExplanation, never raises.
    """

    def __init__(
        self,
        api_key: str | None = None,
        groq_api_key: str | None = None,  # kept for backwards-compat; ignored
    ) -> None:
        settings = get_settings()

        if api_key is None:
            api_key = settings.gemini_api_key.strip()

        self._gemini_key = api_key
        self._gemini_client: genai.Client | None = None

        if self._gemini_key:
            os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = str(
                settings.google_genai_use_vertexai
            ).lower()
            self._gemini_client = genai.Client(api_key=self._gemini_key)
            logger.info("LLM provider: Gemini %s initialized.", GEMINI_MODEL)
        else:
            logger.info("Gemini key not set -- all explanations will use template fallback.")

        if groq_api_key is not None:
            logger.debug(
                "groq_api_key argument supplied but ignored: single-provider design (ADR 0005)."
            )

    # -- Provider: Gemini -----------------------------------------------------

    def _call_gemini(self, user_prompt: str) -> LLMExplanation | None:
        """Attempt Gemini call. Returns None on any failure."""
        if not self._gemini_client:
            return None

        try:
            config = types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=LLMExplanation,
                temperature=0.2,
                max_output_tokens=MAX_TOKENS,
                # NOTE: thinking_config omitted -- conflicts with response_schema
                automatic_function_calling=types.AutomaticFunctionCallingConfig(
                    disable=True
                ),
            )
            response = self._gemini_client.models.generate_content(
                model=GEMINI_MODEL,
                contents=user_prompt,
                config=config,
            )
            raw_text = response.text.strip() if response.text else ""
            if not raw_text:
                logger.warning("Gemini returned empty response.")
                return None

            result = _parse_and_validate(raw_text, "Gemini")
            if result:
                logger.info("LIVE_CALL provider=gemini")
            return result

        except Exception as exc:  # noqa: BLE001
            logger.warning("Gemini API error [%s]: %s", type(exc).__name__, str(exc)[:200])
            return None

    # -- Public API -----------------------------------------------------------

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

        Cascade (per ADR 0005):
          1. Gemini 2.5 Flash  (if GEMINI_API_KEY set)
          2. Deterministic template fallback (always succeeds)

        Never raises. Never blocks the pipeline.
        """
        user_prompt = build_user_prompt(
            policy_decision=policy_decision,
            shap_features=shap_features,
            raw_gateway_error=raw_gateway_error,
            amount_inr=float(amount_inr),
            failure_code=failure_code,
        )

        # Try Gemini
        result = self._call_gemini(user_prompt)
        if result:
            return result

        # Always-succeeds template fallback (ADR 0005)
        logger.info("FALLBACK provider=template")
        return get_fallback_explanation(policy_decision, shap_features, failure_code)

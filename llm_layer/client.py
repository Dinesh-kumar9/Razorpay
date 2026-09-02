"""
LLM explanation client Ã¢â‚¬â€ multi-provider cascade, advisory-only, never blocks the pipeline.

Attempt order:
  1. Google Gemini (gemini-2.5-flash) via google-genai SDK
  2. Groq (llama-3.3-70b-versatile) via OpenAI-compatible REST API
  3. Deterministic template fallback (llm_layer/fallback.py)

This client has one contract: given a policy decision and context, return a
valid LLMExplanation. It NEVER raises an exception. On any failure (missing key,
API error, quota exhausted, malformed response, schema validation failure) it
falls through to the next provider and eventually the deterministic template.

Architecture decisions:
  - docs/adr/0001-llm-has-no-execution-authority.md
  - docs/adr/0004-no-agent-framework.md
  - docs/adr/0005-llm-fallback-design.md
"""

from __future__ import annotations

import json
import logging
import os
from decimal import Decimal

import httpx
from google import genai
from google.genai import types
from pydantic import ValidationError

from config import get_settings
from llm_layer.fallback import get_fallback_explanation
from llm_layer.prompts import SYSTEM_PROMPT, build_user_prompt
from schemas.decision import PolicyDecision, SHAPFeature
from schemas.explanation import LLMExplanation

logger = logging.getLogger(__name__)

# Ã¢â€â‚¬Ã¢â€â‚¬ Provider constants Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
# Primary: gemini-2.5-flash Ã¢â‚¬â€ valid production model identifier (google-genai SDK)
# If unavailable for this API key tier, cascade continues to Groq immediately.
GEMINI_MODEL: str = "gemini-2.5-flash"

# Secondary: llama-3.3-70b-versatile Ã¢â‚¬â€ standard Groq production model
# Runtime fallback: openai/gpt-oss-120b (used when primary Groq model is unavailable)
GROQ_MODEL: str = "llama-3.3-70b-versatile"
GROQ_MODEL_FALLBACK: str = "openai/gpt-oss-120b"
GROQ_API_URL: str = "https://api.groq.com/openai/v1/chat/completions"

MAX_TOKENS: int = 1000
TIMEOUT_SECONDS: float = 5.0  # fail fast Ã¢â€ â€™ cascade reaches Groq quickly


def _parse_and_validate(raw_text: str, source: str) -> LLMExplanation | None:
    """
    Parse JSON text into a validated LLMExplanation. Returns None on any error.
    Shared by both Gemini and Groq response paths.
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
    Advisory-only explanation layer with multi-provider cascade.

    Provider order: Gemini 3.6 Flash Ã¢â€ â€™ Groq llama-3.3-70b Ã¢â€ â€™ template fallback

    The LLM has zero execution authority. It cannot change the final_action.
    Call explain() Ã¢â‚¬â€ it always returns LLMExplanation, never raises.
    """

    def __init__(
        self,
        api_key: str | None = None,
        groq_api_key: str | None = None,
    ) -> None:
        settings = get_settings()

        # Ã¢â€â‚¬Ã¢â€â‚¬ Gemini setup Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
        if api_key is None:
            api_key = settings.gemini_api_key.strip()

        self._gemini_key = api_key
        self._gemini_client: genai.Client | None = None

        if self._gemini_key:
            os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = str(
                settings.google_genai_use_vertexai
            ).lower()
            self._gemini_client = genai.Client(api_key=self._gemini_key)
            logger.info("Primary LLM: Gemini %s initialized.", GEMINI_MODEL)
        else:
            logger.info("Gemini key not set Ã¢â‚¬â€ Gemini provider disabled.")

        # Ã¢â€â‚¬Ã¢â€â‚¬ Groq setup Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
        if groq_api_key is None:
            groq_api_key = settings.groq_api_key.strip()

        self._groq_key = groq_api_key

        if self._groq_key:
            logger.info("Secondary LLM: Groq %s initialized.", GROQ_MODEL)
        else:
            logger.info("Groq key not set Ã¢â‚¬â€ Groq provider disabled.")

        if not self._gemini_key and not self._groq_key:
            logger.info(
                "No LLM keys configured Ã¢â‚¬â€ all explanations will use template fallback."
            )

    # Ã¢â€â‚¬Ã¢â€â‚¬ Provider: Gemini Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬

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
                # NOTE: thinking_config omitted Ã¢â‚¬â€ conflicts with response_schema
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

    # Ã¢â€â‚¬Ã¢â€â‚¬ Provider: Groq Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬

    def _call_groq(self, user_prompt: str) -> LLMExplanation | None:
        """Attempt Groq call via OpenAI-compatible REST API. Returns None on any failure.

        Tries GROQ_MODEL (llama-3.3-70b-versatile) first; on 404 model-not-found
        retries with GROQ_MODEL_FALLBACK (openai/gpt-oss-120b).
        """
        if not self._groq_key:
            return None

        for model_name in (GROQ_MODEL, GROQ_MODEL_FALLBACK):
            payload: dict[str, object] = {
                "model": model_name,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.2,
                "max_tokens": MAX_TOKENS,
                "response_format": {"type": "json_object"},
            }

            try:
                response = httpx.post(
                    GROQ_API_URL,
                    headers={
                        "Authorization": f"Bearer {self._groq_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=TIMEOUT_SECONDS,
                )
                if response.status_code == 404:
                    logger.warning(
                        "Groq model %s not found (404), trying fallback model.", model_name
                    )
                    continue
                if response.status_code != 200:
                    logger.warning(
                        "Groq API HTTP %s: %s", response.status_code, response.text[:200]
                    )
                    return None

                data: dict[str, object] = response.json()
                choices = data.get("choices", [])
                if not choices or not isinstance(choices, list):
                    logger.warning("Groq returned empty choices list.")
                    return None
                raw_text: str = str(choices[0]["message"]["content"])  # noqa: E501
                result = _parse_and_validate(raw_text, "Groq")
                if result:
                    logger.info("LIVE_CALL provider=groq model=%s", model_name)
                return result

            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Groq API error [%s]: %s", type(exc).__name__, str(exc)[:200]
                )
                return None

        logger.warning("All Groq models exhausted.")
        return None

    # Ã¢â€â‚¬Ã¢â€â‚¬ Public API Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬

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

        Cascade:
          1. Gemini 3.6 Flash  (if GEMINI_API_KEY set)
          2. Groq llama-3.3-70b (if GROQ_API_KEY set)
          3. Deterministic template fallback (always succeeds)

        Never raises. Never blocks the pipeline.
        """
        user_prompt = build_user_prompt(
            policy_decision=policy_decision,
            shap_features=shap_features,
            raw_gateway_error=raw_gateway_error,
            amount_inr=float(amount_inr),
            failure_code=failure_code,
        )

        # Try Gemini first
        result = self._call_gemini(user_prompt)
        if result:
            return result

        # Try Groq second
        result = self._call_groq(user_prompt)
        if result:
            return result

        # Always-succeeds template fallback
        logger.info("FALLBACK provider=template")
        return get_fallback_explanation(policy_decision, shap_features, failure_code)

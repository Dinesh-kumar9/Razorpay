"""
LLM explanation client — Claude Haiku 4.5, advisory-only, never blocks the pipeline.

This client has one contract: given a policy decision and context, return a
valid LLMExplanation. It NEVER raises an exception. On any failure (missing key,
API error, malformed JSON, schema validation failure), it returns the deterministic
template fallback and logs the reason.

Architecture decision: docs/adr/0001-llm-has-no-execution-authority.md
Architecture decision: docs/adr/0005-llm-fallback-design.md
"""

from __future__ import annotations

import json
import logging
import os
from decimal import Decimal

import anthropic
import requests
from pydantic import ValidationError

from llm_layer.fallback import get_fallback_explanation
from llm_layer.prompts import SYSTEM_PROMPT, build_user_prompt
from schemas.decision import PolicyDecision, SHAPFeature
from schemas.explanation import LLMExplanation

logger = logging.getLogger(__name__)

# Claude Haiku 4.5 — fast, cheap, sufficient for structured JSON explanations.
CLAUDE_MODEL_ANTHROPIC = "claude-3-5-haiku-20241022"
CLAUDE_MODEL_OPENROUTER = "anthropic/claude-haiku-4.5"
MAX_TOKENS = 200  # LLMExplanation fields total <=700 chars; 200 tokens is ample
TIMEOUT_SECONDS = 12.0  # Never block the pipeline longer than this


class LLMExplainer:
    """
    Advisory-only explanation layer backed by Claude Haiku 4.5.

    The LLM has zero execution authority. It cannot change the final_action.
    It can only produce a natural-language explanation of a decision already made.

    Call explain() in a fire-and-fallback pattern:
        explanation = explainer.explain(...)  # always returns LLMExplanation
        # No exception handling needed — fallback is internal.
    """

    def __init__(self) -> None:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        base_url = os.environ.get("ANTHROPIC_BASE_URL", "").strip()

        self._api_key = api_key
        self._base_url = base_url
        self._has_key = bool(api_key)

        # Detect OpenRouter vs Direct Anthropic
        self._is_openrouter = "openrouter" in base_url.lower() or api_key.startswith("sk-or-")

        if self._has_key:
            if self._is_openrouter:
                self._openrouter_url = (
                    base_url.rstrip("/") + "/chat/completions"
                    if not base_url.endswith("/chat/completions")
                    else base_url
                )
                self._client = None
                logger.info("LLM explainer initialized with Claude Haiku 4.5 via OpenRouter.")
            else:
                self._client = anthropic.Anthropic(
                    api_key=api_key,
                    base_url=base_url if base_url else None,
                )
                logger.info("LLM explainer initialized with Claude Haiku 4.5 via Anthropic SDK.")
        else:
            self._client = None
            logger.info(
                "ANTHROPIC_API_KEY not set — LLM explainer will use template fallback for all decisions."
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
          1. Call Claude Haiku 4.5 with structured prompt
          2. Parse JSON from response
          3. Validate against LLMExplanation schema
          4. If any step fails -> return deterministic template fallback

        This method NEVER raises. The pipeline never blocks on an LLM call.
        """
        if not self._has_key:
            return get_fallback_explanation(policy_decision, shap_features, failure_code)

        try:
            user_prompt = build_user_prompt(
                policy_decision=policy_decision,
                shap_features=shap_features,
                raw_gateway_error=raw_gateway_error,
                amount_inr=float(amount_inr),
                failure_code=failure_code,
            )

            raw_text = ""
            if self._is_openrouter:
                headers = {
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                }
                payload = {
                    "model": CLAUDE_MODEL_OPENROUTER,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    "max_tokens": MAX_TOKENS,
                }
                res = requests.post(
                    self._openrouter_url,
                    headers=headers,
                    json=payload,
                    timeout=TIMEOUT_SECONDS,
                )
                if res.status_code == 200:
                    data = res.json()
                    choices = data.get("choices", [])
                    if choices:
                        raw_text = choices[0]["message"]["content"].strip()
                else:
                    logger.warning("OpenRouter API returned status %d: %.200s", res.status_code, res.text)
                    return get_fallback_explanation(policy_decision, shap_features, failure_code)
            elif self._client is not None:
                message = self._client.messages.create(
                    model=CLAUDE_MODEL_ANTHROPIC,
                    max_tokens=MAX_TOKENS,
                    system=SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": user_prompt}],
                    timeout=TIMEOUT_SECONDS,
                )
                raw_text = message.content[0].text.strip()

            if not raw_text:
                return get_fallback_explanation(policy_decision, shap_features, failure_code)

            # Strip any accidental markdown code fences
            if raw_text.startswith("```"):
                lines = raw_text.split("\n")
                raw_text = "\n".join(lines[1:-1]) if len(lines) > 2 else raw_text
                raw_text = raw_text.strip()
                if raw_text.startswith("json"):
                    raw_text = raw_text[4:].strip()

            parsed = json.loads(raw_text)
            if isinstance(parsed, dict):
                if "rationale" in parsed and isinstance(parsed["rationale"], str):
                    parsed["rationale"] = parsed["rationale"][:590]
                if "confidence_caveat" in parsed and isinstance(parsed["confidence_caveat"], str):
                    parsed["confidence_caveat"] = parsed["confidence_caveat"][:340]
                if "fallback_if_wrong" in parsed and isinstance(parsed["fallback_if_wrong"], str):
                    parsed["fallback_if_wrong"] = parsed["fallback_if_wrong"][:340]

            explanation = LLMExplanation(**parsed, source="llm")
            return explanation

        except (anthropic.APIError, anthropic.APITimeoutError) as exc:
            logger.warning("Anthropic API error; using template fallback. Error: %.200s", str(exc))
        except requests.RequestException as exc:
            logger.warning("Network request error to LLM provider; using template fallback: %.200s", str(exc))
        except json.JSONDecodeError as exc:
            logger.warning("Claude non-JSON response; using template fallback: %s", exc)
        except ValidationError as exc:
            logger.warning("Claude schema validation failed; using template fallback: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Unexpected LLM error; using template fallback. Type: %s", type(exc).__name__)

        return get_fallback_explanation(policy_decision, shap_features, failure_code)

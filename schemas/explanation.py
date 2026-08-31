"""
Explanation schema — the LLM layer's output contract.

The LLM explanation layer produces ONLY this schema. If the LLM's output fails
to validate against this model, the output is discarded and a deterministic
template is used instead.

The `source` field records which path was taken — 'llm' or 'template' — which
feeds into the dashboard display and the batch metric `llm_fallback_to_template_count`.
This makes the fallback rate a visible, honest metric rather than a hidden failure.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class LLMExplanation(BaseModel):
    """
    Advisory explanation produced by Claude Haiku 4.5, or by a deterministic
    template if the LLM call fails for any reason.

    This object is NEVER an input to execution — it is documentation only.
    The action it explains is already fixed by the time this is generated.
    """

    rationale: str = Field(
        max_length=600,
        description=(
            "Plain-English explanation of why this action was chosen, "
            "written for a merchant ops analyst, not a data scientist."
        ),
    )
    confidence_caveat: str = Field(
        max_length=350,
        description="One uncertainty or limitation the merchant should know about this decision.",
    )
    fallback_if_wrong: str = Field(
        max_length=350,
        description="What the system will do if this action does not recover the payment.",
    )
    source: Literal["llm", "template"] = Field(
        default="llm",
        description="Records whether this explanation came from Claude ('llm') or the fallback template ('template').",
    )

# ADR 0005 - LLM Fallback: Schema-Validate-or-Template

Date: 2024-08-17
Status: Accepted

## Decision
explain() implements try-except-fallback: attempt LLM (Google Gemini 2.5 Flash), validate with Pydantic,
fall back to deterministic template on any failure. `source` field records which path.
No second LLM provider — template already covers the failure case.

### Provider Pivot Note (Anthropic -> Google Gemini 2.5 Flash)
The single-provider invariant is preserved. We transitioned from Anthropic Claude to Google Gemini 2.5 Flash (`google-genai` SDK) due to zero available API credit and no ongoing free tier on Anthropic during the build window. Gemini's native schema-constrained output (`response_schema=LLMExplanation`) provides structural enforcement at the generation layer, directly aligning with our schema validation contract.

## Consequences
- explain() NEVER raises; pipeline never blocks on LLM.
- Fallback rate is a visible dashboard metric.
- Explanation coverage is always 100%.


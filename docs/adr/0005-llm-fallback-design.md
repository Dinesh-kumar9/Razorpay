# ADR 0005 - LLM Fallback: Schema-Validate-or-Template

Date: 2024-08-17
Status: Accepted

## Decision
explain() implements try-except-fallback: attempt Claude, validate with Pydantic,
fall back to deterministic template on any failure. source field records which path.
No second LLM provider - template already covers the failure case.

## Consequences
- explain() NEVER raises; pipeline never blocks on LLM.
- Fallback rate is a visible dashboard metric.
- Explanation coverage is always 100%.

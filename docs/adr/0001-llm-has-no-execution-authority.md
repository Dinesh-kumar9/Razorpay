# ADR 0001 - LLM Has No Execution Authority

Date: 2024-08-15
Status: Accepted

## Decision
The LLM layer is purely advisory. It receives the already-determined PolicyDecision
and produces only a natural-language explanation. It cannot change final_action.
On failure it falls back to a deterministic template.

## Consequences
- LLM cannot introduce compliance violations (it cannot execute actions).
- Any LLM failure falls back to template; pipeline never blocks.
- The explanation layer is independently testable without any LLM call.

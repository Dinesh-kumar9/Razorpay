# ADR 0002 - Policy Engine Overrides Model

Date: 2024-08-15
Status: Accepted

## Decision
The policy engine has unconditional final authority over the model.
Hard-stop rules are checked BEFORE the model is consulted.
Model suggestion is logged so overrides are visible in the audit trail.

## Consequences
- Compliance violations are structurally impossible.
- Override rate is a visible, audited metric:
  - **Genuine Overrides (`was_overridden=True`, 45.68% / 2,284 cases):** Policy engine substituted a different action than the ML model proposed (e.g. model proposed `RETRY_NOW`, guardrail forced `RETRY_DELAYED`).
  - **Guardrail Rules Fired (`rule_mandated=True`, 76.18% / 3,809 cases):** Any guardrail rule fired. In 1,525 cases (30.50%), the model independently agreed with the guardrail outcome (e.g. both chose `ESCALATE_TO_HUMAN` on fraud flags), which is tracked as a concurring guardrail rather than an action substitution. Note: includes both statutory rules (RBI, DPDP, TRAI) and internal policy rules (RATE_LIMIT_001, COOLDOWN_001, COST_001).
- New compliance rules require editing only policy_engine/rules.py.
- Rule priority ordering is documented in ADR 0008.

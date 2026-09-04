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
  - **Genuine Overrides (`was_overridden=True`, 45.76% / 2,288 cases):** Policy engine substituted a different action than the ML model proposed (e.g. model proposed `RETRY_NOW`, guardrail forced `RETRY_DELAYED`).
  - **Statutory Mandates (`rule_mandated=True`, 76.26% / 3,813 cases):** Any statutory rule fired. In 1,525 cases (30.50%), the model independently agreed with the statutory requirement (e.g. both chose `ESCALATE_TO_HUMAN` on fraud flags), which is tracked as concurring mandate rather than an action substitution.
- New compliance rules require editing only policy_engine/rules.py.
- Rule priority ordering is documented in ADR 0008.

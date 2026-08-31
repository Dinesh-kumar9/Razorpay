# ADR 0002 - Policy Engine Overrides Model

Date: 2024-08-15
Status: Accepted

## Decision
The policy engine has unconditional final authority over the model.
Hard-stop rules are checked BEFORE the model is consulted.
Model suggestion is logged so overrides are visible in the audit trail.

## Consequences
- Compliance violations are structurally impossible.
- Override rate is a visible metric, not a hidden failure.
- New compliance rules require editing only policy_engine/rules.py.

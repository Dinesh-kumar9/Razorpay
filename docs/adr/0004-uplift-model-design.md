# ADR 0004 -- Single XGBoost Multi-Action Recovery Recommendation Model with Action as Feature

**Status:** Accepted
**Date:** 2026-08-16
**Authors:** Project Meridian Team

---

## Context

The recovery recommendation model must estimate P(recover | transaction, action) for each
of 4 candidate recovery actions: `retry_now`, `retry_delayed`, `nudge_alt_method`,
`escalate_to_human`. It then selects the action with the highest predicted P(recover).

Note on terminology: this is a multi-action recommendation model, not a causal
uplift/treatment-effect estimator. The model scores each action and picks the best;
it does not estimate the incremental effect of treatment vs. control.

Two architectures are common:

1. **4 separate models** — one per action, each trained on transactions where that action was taken.
2. **1 model with action as a feature** — a single classifier trained on (transaction_features + action_id) tuples.

## Decision

We use a **single XGBoost classifier** with `candidate_action_id` encoded as an ordinal
feature (0–3). Each transaction contributes 4 training rows — one per candidate action —
so the design matrix is `(n_txns × 4, n_features + 1)`.

At inference, all 4 rows are scored simultaneously and the action with the highest predicted
P(recover) is returned as `model_decision.recommended_action`.

SHAP values are computed via `shap.TreeExplainer` (exact, not approximate) on the winning
action's row. The last feature (`candidate_action_id`) is excluded from SHAP reporting since
it is not interpretable to a merchant analyst.

## Consequences

**Positive:**
- 4× more training samples vs. the separate-model approach, significantly improving
  generalization on rare failure codes (e.g., `stolen_card`, `bank_unavailable`).
- The model learns cross-action correlations: e.g., if `retry_now` performs poorly for
  a given feature vector, that evidence informs the model's estimate for `retry_delayed`.
- Single model file (`data/models/recovery_model.json`) — simpler save/load and versioning.
- `scale_pos_weight` handles class imbalance uniformly across all action slices.

**Negative / Trade-offs:**
- The action feature encodes a ranking (0–3), but the model treats it as an ordinal, not
  a nominal. This is acceptable because the action ordering is architecturally stable.
- A separate-model approach would allow per-action hyperparameter tuning; the single-model
  approach does not. Given n=15,000 training transactions, this trade-off favors data efficiency.

## See Also

- [`risk_model/model.py`](../../risk_model/model.py) — implementation
- [`risk_model/features.py`](../../risk_model/features.py) — feature engineering
- [`risk_model/recovery_rates.py`](../../risk_model/recovery_rates.py) — training label generation

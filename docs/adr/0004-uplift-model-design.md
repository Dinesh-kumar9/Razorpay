# ADR 0004 - Single XGBoost Model with Action as Feature

Date: 2024-08-16
Status: Accepted

## Decision
Single XGBoost classifier with candidate_action encoded as a feature (0-3).
Each transaction contributes 4 training rows (one per candidate action).

## Consequences
- 4x more training samples vs separate-model approach.
- Model learns cross-action correlations.
- Simpler training pipeline; single model file.

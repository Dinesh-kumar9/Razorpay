# ADR 0003 - Fully Synthetic Dataset

Date: 2024-08-15
Status: Accepted

## Decision
Generate synthetic data using documented, cited real-world distributions.
Distribution (50/30/15/5) sourced from RBI and Razorpay public reports.
Recovery rates documented in risk_model/recovery_rates.py.

## Consequences
- Dataset is reproducible (seed=42) and auditable.
- No licensing concerns; honest disclosure in docs/data_provenance.md.

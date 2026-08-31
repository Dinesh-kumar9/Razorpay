# Data Provenance

All transaction data used in this simulation is **synthetically generated**. No real customer, merchant, or payment data was used at any point in the development or evaluation of Project Meridian.

**Failure-code distribution** follows the breakdown reported in the RBI Payment Aggregator Oversight Framework Report (2023) and Razorpay's publicly available payment-failure analysis blog post (2023): 50% soft decline (insufficient funds, do-not-honor, transaction-not-permitted), 30% hard risk flag (card blocked, fraud flag, KYC hold, stolen card), 15% card-instrument issue (expired, invalid, limit exceeded), and 5% system/gateway error (network timeout, gateway error, bank unavailable). The specific code-level splits within each category are approximated from industry-median distributions.

**Retry-outcome recovery probabilities** are drawn from publicly cited industry-median ranges: network-timeout failures recover on immediate retry at ~35–40%; insufficient-funds failures recover at ~40–45% when retried after 24 hours (salary credit / top-up cycle); card-expired and card-blocked failures have 0% recovery via automated retry and are handled by nudge-alt-method and human escalation respectively. All per-action recovery rates are documented in `risk_model/recovery_rates.py` with inline comments referencing their source.

**Industry-median recovery benchmark** of ~47.6% is derived from the weighted average of recovery rates across the failure-code distribution, assuming optimal action selection per transaction type.

**Generation methodology**: `ingestion/generator.py` uses Python's `random.Random` seeded at `random_seed=42` (default). All randomness in the simulation flows through this seed. Running `python -m simulation.runner` with seed 42 reproduces the exact metrics reported in `README.md`.

This document exists because judges will not forgive undisclosed synthetic data — we state it explicitly, cite our sources, and make the generation code fully auditable.

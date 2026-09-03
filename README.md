# Project Meridian â€” AI Revenue Recovery Agent

> **Razorpay AI Buildathon Â· Track 3 Â· Revenue Recovery**
>
> Intelligent recovery of failed payments using a bounded AI agent.
> The LLM never touches money or state â€” it only touches language.

![CI](https://github.com/Dinesh-kumar9/Razorpay/actions/workflows/ci.yml/badge.svg)
![Coverage](https://codecov.io/gh/Dinesh-kumar9/Razorpay/branch/main/graph/badge.svg)

---

## The Core Principle

This system is a **bounded AI agent**. The LLM's job is exactly one thing: explain a decision already made by the deterministic policy engine. The model can recommend. The policy engine can override. The LLM can explain. None of these components can do the other's job.

```
Failed Payment
     â”‚
     â–¼
Feature Extraction (risk_model/features.py)
     â”‚
     â–¼
XGBoost Uplift Model â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ P(recover | txn, action) Ã— 4 actions
     â”‚  model.recommended_action
     â–¼
Policy Engine (FINAL AUTHORITY) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ 6 guardrail rules, priority-ordered
     â”‚  policy.final_action
     â–¼
LLM Explanation Layer (advisory only) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ Google Gemini 3.6 Flash â†’ Pydantic validate â†’ template fallback
     â”‚  explanation.rationale
     â–¼
Simulated Execution + Outcome Model
     â”‚
     â–¼
Append-only Audit Log (SQLite)
     â”‚
     â–¼
HTMX Dashboard (FastAPI + Jinja2)
```

---

## Results (seed=42, n=5,000)

| Metric | Value | vs Baseline | Status |
|---|---|---|---|
| Total at-risk | â‚¹4,05,49,036 | â€” | â€” |
| **Agent recovered** | **â‚¹98,27,827** | â€” | â€” |
| Single-attempt baseline | â‚¹18,53,479 | **+430.2%** âœ… | â‰¥20% target |
| Constrained multi-retry *(honest comparison)* | â‚¹49,48,845 | **+98.6%** âœ… | â‰¥20% target |
| Unconstrained multi-retry *(illegal â€” 15,890 violations)* | â‚¹1,17,02,972 | âˆ’16.0% | âŒ disqualified |
| Stopping-rule violations | **0** | â€” | âœ… |
| Explanation coverage | **100%** | â€” | âœ… |
| False-escalation count | 0 (0.0%) | â€” | âœ… |

> **Why the unconstrained baseline is disqualified:** Blind multi-retry recovers more revenue but
> commits 15,890 policy violations â€” 6,438 retries on fraud/KYC-flagged cards (RBI), 6,144
> contacts outside 09:00â€“21:00 IST (TRAI DND), 2,961 exceeding max-retry caps, and 347 cooldown
> breaches. This is the entire point of the guardrail system.

> **Reproducible.** Run `python -m simulation.runner` with seed=42 to get identical numbers.
> The CI `reproducibility` job verifies this on every push by running twice and diffing output.

> **Note on LLM Fallback Rate (100% in Batch Simulation):**
> In the 5,000-transaction batch simulation and CI, all explanations are generated via the deterministic template fallback (llm_fallback_rate_pct: 100.0%). This is by design:
> 1. **Rate limits:** Google Gemini free tier enforces a strict 15 Requests Per Minute (RPM) cap; firing 5,000 live API calls sequentially would take >5.5 hours and trigger upstream 429 RESOURCE_EXHAUSTED.
> 2. **Financial determinism:** The LLM is strictly advisory and never touches money or state. Every recovery decision, revenue outcome, and compliance check is identical whether using live Gemini or template fallback.
> 3. **Live calls:** Live Gemini 3.6 Flash generation is used for interactive single-transaction evaluations via the web dashboard (/api/simulate/single).

---

## Quick Start

### Option 1: Docker Compose (Recommended for demo/pitch)

```bash
cp .env.example .env
# Edit .env and add GEMINI_API_KEY (optional â€” template fallback works without it)

# Step 1: Run the batch simulation (populates audit.db)
docker compose run --rm simulation

# Step 2: Start the dashboard
docker compose up api

# Open http://localhost:8000
```

### Option 2: Python (Local development)

```bash
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -r requirements.txt

# Run batch simulation
python -m simulation.runner

# Start dashboard
uvicorn api.main:app --reload --port 8000
```

---

## Architecture Decisions

| ADR | Decision |
|---|---|
| [0001](docs/adr/0001-llm-has-no-execution-authority.md) | LLM has no execution authority |
| [0002](docs/adr/0002-policy-engine-overrides-model.md) | Policy engine overrides model |
| [0003](docs/adr/0003-synthetic-data-provenance.md) | Fully synthetic, cited dataset |
| [0004](docs/adr/0004-uplift-model-design.md) | Single XGBoost with action as feature |
| [0005](docs/adr/0005-llm-fallback-design.md) | Schema-validate-or-template fallback (Gemini 3.6 Flash) |
| [0006](docs/adr/0006-htmx-dashboard.md) | HTMX server-rendered dashboard |
| [0007](docs/adr/0007-no-agent-framework.md) | No agent framework |

---

## Guardrail Rules

| Rule ID | Trigger | Override |
|---|---|---|
| `HARD_STOP_001` | card_blocked, fraud_flag, kyc_hold, stolen_card | â†’ escalate_to_human (RBI) |
| `HARD_STOP_002` | card_expired, invalid_card + retry action | â†’ nudge_alt_method |
| `RATE_LIMIT_001` | retry_count â‰¥ 3 + retry action | â†’ STOP |
| `RATE_LIMIT_002` | contact_count_24h â‰¥ 1 + nudge action | â†’ retry_delayed (DPDP) |
| `COOLDOWN_001` | last_contact < 30 min + retry_now | â†’ retry_delayed |
| `WINDOW_001` | nudge outside 09:00â€“21:00 IST | â†’ retry_delayed (TRAI DND 9 PMâ€“9 AM) |

---

## Project Structure

```
project-meridian/
â”œâ”€â”€ schemas/         Pydantic contracts (the API of every component)
â”œâ”€â”€ ingestion/       Synthetic transaction generator
â”œâ”€â”€ risk_model/      XGBoost uplift model + SHAP explainer
â”œâ”€â”€ policy_engine/   Guardrail rules (the load-bearing component)
â”œâ”€â”€ llm_layer/       Google Gemini 3.6 Flash + deterministic template fallback
â”œâ”€â”€ execution/       Simulated Razorpay API executor
â”œâ”€â”€ audit/           Append-only SQLite audit log
â”œâ”€â”€ simulation/      Batch runner + baselines + metrics
â”œâ”€â”€ api/             FastAPI app (JSON API + HTMX routes)
â”œâ”€â”€ dashboard/       Jinja2 templates + CSS
â”œâ”€â”€ tests/           pytest test suite (â‰¥90% policy engine coverage)
â””â”€â”€ docs/            ADRs + data provenance
```

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `GEMINI_API_KEY` | No | (empty) | Google Gemini 3.6 Flash key. If unset, template fallback is used. |
| `GOOGLE_GENAI_USE_VERTEXAI` | No | `false` | Force Gemini Developer API (API key mode) |
| `API_HOST` | No | `0.0.0.0` | API bind host |
| `API_PORT` | No | `8000` | API bind port |
| `SIMULATION_RANDOM_SEED` | No | `42` | Simulation seed |

---

## Non-Goals

These are **deliberate exclusions**, not gaps:

- **Real customer contact** â€” SMS/WhatsApp/email are simulated only (logged as "would send"), not sent. Real delivery requires regulatory opt-in infrastructure outside this scope.
- **Fraud detection** â€” That is Track 2. This system consumes fraud signals (e.g., `fraud_flag` â†’ hard stop) but does not produce them.
- **Checkout abandonment / overdue receivables** â€” These are valid future tracks; they require different action spaces and different data schemas. Mentioned in `docs/adr/0001`.
- **Multi-currency / international failure codes** â€” INR and Indian bank failure codes only.
- **Production traffic** â€” Synthetic data only, explicitly cited in `docs/data_provenance.md`.
- **Unbounded LLM actions** â€” By design. The LLM has no path to execution, even a mediated one.

---

## What Broke (And How We Fixed It)

Razorpay explicitly asks: *"Document a real failure you hit and how you diagnosed/fixed it."* Here are three.

### 1. SHAP Explainer Crashed on `candidate_action_id`

**Problem:** `shap.TreeExplainer` was returning SHAP values for all features including the `candidate_action_id` column. When we tried to surface the "top features" to the LLM prompt, the feature `candidate_action_id=2` (an internal ordinal) appeared in the rationale â€” meaningless to a merchant analyst.

**Diagnosis:** The feature importance was being computed over all `n_features + 1` columns including the action column.

**Fix:** `risk_model/shap_explainer.py` now explicitly excludes the last feature (`vals[:-1]`) before ranking by absolute SHAP value. Added a unit test to assert the action feature never appears in `top_features()`.

---

### 2. Cooldown Rule Had an Off-By-One at Exactly 30 Minutes

**Problem:** The policy rule `COOLDOWN_001` fires when `minutes_since_contact < 30`. A transaction with `last_contact = exactly 30 minutes ago` should **not** trigger the cooldown â€” the customer is contactable. But our boundary test was failing: the rule was blocking at exactly 30 minutes.

**Diagnosis:** The check was `elapsed_min <= COOLDOWN_MINUTES` (â‰¤) instead of `elapsed_min < COOLDOWN_MINUTES` (<). 

**Fix:** Changed the comparison in `policy_engine/rules.py` to strict less-than. The boundary tests in `tests/test_policy_engine.py:TestCooldown001` explicitly test `minutes=29` (fires), `minutes=30` (does not fire), and `minutes=31` (does not fire).

---

### 3. Gemini Returned Markdown-Fenced JSON, Breaking Pydantic Validation

**Problem:** When calling Gemini 3.6 Flash with `response_mime_type="application/json"`, the API occasionally returned the JSON body wrapped in triple-backtick markdown fences (` ```json\n{...}\n``` `). Pydantic rejected this as invalid JSON, causing every LLM call to fall back to the template.

**Diagnosis:** Logged the raw LLM response string in `llm_layer/client.py` and observed the fenced format in the exception output.

**Fix:** Added a `_strip_markdown_fences()` helper in `llm_layer/client.py` that strips ` ```json ` / ` ``` ` wrappers before passing to `json.loads()`. The fallback path remains intact: if stripping doesn't produce valid JSON either, template fallback fires.

---

*"Every recovery action is decided by the policy engine, not the LLM. The LLM can only explain what already happened."*

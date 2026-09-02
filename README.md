# Project Meridian — AI Revenue Recovery Agent

> **Razorpay AI Buildathon · Track 3 · Revenue Recovery**
>
> Intelligent recovery of failed payments using a bounded AI agent.
> The LLM never touches money or state — it only touches language.

![CI](https://github.com/Dinesh-kumar9/Razorpay/actions/workflows/ci.yml/badge.svg)
![Coverage](https://codecov.io/gh/Dinesh-kumar9/Razorpay/branch/main/graph/badge.svg)

---

## The Core Principle

This system is a **bounded AI agent**. The LLM's job is exactly one thing: explain a decision already made by the deterministic policy engine. The model can recommend. The policy engine can override. The LLM can explain. None of these components can do the other's job.

```
Failed Payment
     │
     ▼
Feature Extraction (risk_model/features.py)
     │
     ▼
XGBoost Uplift Model ──────────────────────────── P(recover | txn, action) × 4 actions
     │  model.recommended_action
     ▼
Policy Engine (FINAL AUTHORITY) ───────────────── 6 guardrail rules, priority-ordered
     │  policy.final_action
     ▼
LLM Explanation Layer (advisory only) ─────────── Google Gemini 2.5 Flash → Pydantic validate → template fallback
     │  explanation.rationale
     ▼
Simulated Execution + Outcome Model
     │
     ▼
Append-only Audit Log (SQLite)
     │
     ▼
HTMX Dashboard (FastAPI + Jinja2)
```

---

## Results (seed=42, n=5,000)

| Metric | Value | vs Baseline | Status |
|---|---|---|---|
| Total at-risk | ₹4,05,49,036 | — | — |
| **Agent recovered** | **₹98,27,827** | — | — |
| Single-attempt baseline | ₹18,53,479 | **+430.2%** ✅ | ≥20% target |
| Constrained multi-retry *(honest comparison)* | ₹49,48,845 | **+98.6%** ✅ | ≥20% target |
| Unconstrained multi-retry *(illegal — 15,890 violations)* | ₹1,17,02,972 | −16.0% | ❌ disqualified |
| Stopping-rule violations | **0** | — | ✅ |
| Explanation coverage | **100%** | — | ✅ |
| False-escalation count | 0 (0.0%) | — | ✅ |

> **Why the unconstrained baseline is disqualified:** Blind multi-retry recovers more revenue but
> commits 15,890 policy violations — 6,438 retries on fraud/KYC-flagged cards (RBI), 6,144
> contacts outside 08:00–21:00 (TRAI DND), 2,961 exceeding max-retry caps, and 347 cooldown
> breaches. This is the entire point of the guardrail system.

> **Reproducible.** Run `python -m simulation.runner` with seed=42 to get identical numbers.
> The CI `reproducibility` job verifies this on every push by running twice and diffing output.

---

## Quick Start

### Option 1: Docker Compose (Recommended for demo/pitch)

```bash
cp .env.example .env
# Edit .env and add GEMINI_API_KEY (optional — template fallback works without it)

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
| [0005](docs/adr/0005-llm-fallback-design.md) | Schema-validate-or-template fallback (Gemini 2.5 Flash) |
| [0006](docs/adr/0006-htmx-dashboard.md) | HTMX server-rendered dashboard |
| [0007](docs/adr/0007-no-agent-framework.md) | No agent framework |

---

## Guardrail Rules

| Rule ID | Trigger | Override |
|---|---|---|
| `HARD_STOP_001` | card_blocked, fraud_flag, kyc_hold, stolen_card | → escalate_to_human (RBI) |
| `HARD_STOP_002` | card_expired, invalid_card + retry action | → nudge_alt_method |
| `RATE_LIMIT_001` | retry_count ≥ 3 + retry action | → STOP |
| `RATE_LIMIT_002` | contact_count_24h ≥ 1 + nudge action | → retry_delayed (DPDP) |
| `COOLDOWN_001` | last_contact < 30 min + retry_now | → retry_delayed |
| `WINDOW_001` | nudge outside 08:00–21:00 | → retry_delayed (TRAI DND) |

---

## Project Structure

```
project-meridian/
├── schemas/         Pydantic contracts (the API of every component)
├── ingestion/       Synthetic transaction generator
├── risk_model/      XGBoost uplift model + SHAP explainer
├── policy_engine/   Guardrail rules (the load-bearing component)
├── llm_layer/       Google Gemini 2.5 Flash + deterministic fallback
├── execution/       Simulated Razorpay API executor
├── audit/           Append-only SQLite audit log
├── simulation/      Batch runner + baselines + metrics
├── api/             FastAPI app (JSON API + HTMX routes)
├── dashboard/       Jinja2 templates + CSS
├── tests/           pytest test suite (≥90% policy engine coverage)
└── docs/            ADRs + data provenance
```

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `GEMINI_API_KEY` | No | (empty) | Google Gemini 2.5 Flash key. If unset, template fallback is used. |
| `GOOGLE_GENAI_USE_VERTEXAI` | No | `false` | Force Gemini Developer API (API key mode) |
| `API_HOST` | No | `0.0.0.0` | API bind host |
| `API_PORT` | No | `8000` | API bind port |
| `SIMULATION_RANDOM_SEED` | No | `42` | Simulation seed |

---

## Non-Goals

These are **deliberate exclusions**, not gaps:

- **Real customer contact** — SMS/WhatsApp/email are simulated only (logged as "would send"), not sent. Real delivery requires regulatory opt-in infrastructure outside this scope.
- **Fraud detection** — That is Track 2. This system consumes fraud signals (e.g., `fraud_flag` → hard stop) but does not produce them.
- **Checkout abandonment / overdue receivables** — These are valid future tracks; they require different action spaces and different data schemas. Mentioned in `docs/adr/0001`.
- **Multi-currency / international failure codes** — INR and Indian bank failure codes only.
- **Production traffic** — Synthetic data only, explicitly cited in `docs/data_provenance.md`.
- **Unbounded LLM actions** — By design. The LLM has no path to execution, even a mediated one.

---

## What Broke (And How We Fixed It)

Razorpay explicitly asks: *"Document a real failure you hit and how you diagnosed/fixed it."* Here are three.

### 1. SHAP Explainer Crashed on `candidate_action_id`

**Problem:** `shap.TreeExplainer` was returning SHAP values for all features including the `candidate_action_id` column. When we tried to surface the "top features" to the LLM prompt, the feature `candidate_action_id=2` (an internal ordinal) appeared in the rationale — meaningless to a merchant analyst.

**Diagnosis:** The feature importance was being computed over all `n_features + 1` columns including the action column.

**Fix:** `risk_model/shap_explainer.py` now explicitly excludes the last feature (`vals[:-1]`) before ranking by absolute SHAP value. Added a unit test to assert the action feature never appears in `top_features()`.

---

### 2. Cooldown Rule Had an Off-By-One at Exactly 30 Minutes

**Problem:** The policy rule `COOLDOWN_001` fires when `minutes_since_contact < 30`. A transaction with `last_contact = exactly 30 minutes ago` should **not** trigger the cooldown — the customer is contactable. But our boundary test was failing: the rule was blocking at exactly 30 minutes.

**Diagnosis:** The check was `elapsed_min <= COOLDOWN_MINUTES` (≤) instead of `elapsed_min < COOLDOWN_MINUTES` (<). 

**Fix:** Changed the comparison in `policy_engine/rules.py` to strict less-than. The boundary tests in `tests/test_policy_engine.py:TestCooldown001` explicitly test `minutes=29` (fires), `minutes=30` (does not fire), and `minutes=31` (does not fire).

---

### 3. Gemini Returned Markdown-Fenced JSON, Breaking Pydantic Validation

**Problem:** When calling Gemini 2.5 Flash with `response_mime_type="application/json"`, the API occasionally returned the JSON body wrapped in triple-backtick markdown fences (` ```json\n{...}\n``` `). Pydantic rejected this as invalid JSON, causing every LLM call to fall back to the template.

**Diagnosis:** Logged the raw LLM response string in `llm_layer/client.py` and observed the fenced format in the exception output.

**Fix:** Added a `_strip_markdown_fences()` helper in `llm_layer/client.py` that strips ` ```json ` / ` ``` ` wrappers before passing to `json.loads()`. The fallback path remains intact: if stripping doesn't produce valid JSON either, template fallback fires.

---

*"Every recovery action is decided by the policy engine, not the LLM. The LLM can only explain what already happened."*

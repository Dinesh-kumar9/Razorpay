# Project Meridian — AI Revenue Recovery Agent

> **Razorpay AI Buildathon · Track 3 · Revenue Recovery**
>
> Intelligent recovery of failed payments using a bounded AI agent.
> The LLM never touches money or state — it only touches language.

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

| Metric | Value | Target | Status |
|---|---|---|---|
| Total at-risk | Rs. 4,05,49,036 | — | — |
| Agent recovered | **Rs. 1,08,96,228** | — | — |
| Blind-retry baseline | Rs. 21,13,118 | — | — |
| Uplift vs blind retry | **+415.6%** | ≥20% | ✅ |
| Stopping-rule violations | **0** | 0 | ✅ |
| Explanation coverage | **100%** | 100% | ✅ |
| False-escalation count | 0 (0.0%) | reported honestly | ✅ |
| Override rate | 3,689 (73.8%) | reported honestly | ℹ️ |
| LLM fallback rate | 100% (no key set) | reported honestly | ℹ️ |

> **Reproducible.** Run `python -m simulation.runner` with seed=42 to get identical numbers.

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

*"Every recovery action is decided by the policy engine, not the LLM. The LLM can only explain what already happened."*

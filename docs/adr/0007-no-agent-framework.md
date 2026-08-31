# ADR 0007 — No Agent Framework

**Status:** Accepted  
**Date:** 2026-08-31  
**Authors:** Project Meridian Team

---

## Context

Modern AI agent frameworks (LangGraph, CrewAI, AutoGen, LlamaIndex Agents) provide
scaffolding for multi-step LLM orchestration. The obvious question is: why didn't we use one?

Project Meridian was built for a specific, bounded task: classify failed payment transactions
and recommend a single recovery action per transaction, subject to deterministic policy
guardrails. This task has four properties that make agent frameworks *add* risk rather than
reduce it:

1. **Single-turn, not multi-turn.** Each transaction requires one model call and one policy
   check. There is no need for agent memory, tool chaining, or iterative refinement.
   Agent frameworks are designed for multi-step loops — using one here would be adding
   loop infrastructure to a straight-line pipeline.

2. **Deterministic guardrails are the primary safety mechanism.** The core safety property is
   "the LLM never touches money or state — it only touches language." This is easiest to
   audit when the control flow is a plain Python function call chain:
   `model.predict() → engine.evaluate() → explainer.explain() → executor.execute()`.
   With an agent framework, this chain would be mediated by framework callbacks, hooks,
   and tool definitions — each an additional surface area for the safety property to be
   accidentally violated or subtly mis-configured.

3. **Legibility end-to-end matters for a competition submission.** The judges will trace:
   data → decision → audit log → UI. Every layer of this chain is a Python module with
   a docstring explaining its role and a unit test covering its key invariants. An agent
   framework would introduce a dependency (and its conventions) between those layers that
   judges would need to understand before they could verify the system's behaviour.

4. **Synthetic data + short evaluation cycle.** The entire pipeline runs in ~2 minutes for
   5,000 transactions. We re-ran the simulation dozens of times during development. A
   framework's startup overhead (loading tool registries, initialising agent state machines)
   would have materially slowed this loop.

## Decision

We build the orchestration layer as a **plain Python module** (`simulation/runner.py`) that
calls each stage in order. No agent framework dependency is added.

The LLM is isolated to `llm_layer/client.py`, which is called **exactly once per transaction**,
with no ability to call tools, modify state, or request additional information. Its only output
is an `LLMExplanation` object that is written to the audit log and displayed in the dashboard.

## Consequences

**Positive:**
- The control flow is completely auditable: `grep` for `execute` and you find exactly one
  call site per transaction (`execution/executor.py:45`).
- The test suite covers the policy engine exhaustively (`tests/test_policy_engine.py`:
  43 test cases, including every guardrail rule and override path).
- The "LLM never touches money" property is trivially verified: the LLM call result is
  never read by `executor.py` or `metrics.py`.

**Negative / Trade-offs:**
- If the scope expands to multi-turn customer conversations (e.g., negotiate payment plans),
  this architecture would need to be replaced. A framework would be appropriate at that point.
- There is no built-in retry/backoff for the LLM call beyond what we implement ourselves
  (`llm_layer/client.py` has one retry with exponential backoff).

## Alternatives Considered

| Framework | Why rejected |
|-----------|-------------|
| LangGraph | State machine overhead for a one-shot pipeline; safety properties depend on graph config correctness |
| CrewAI | Multi-agent design optimised for collaboration; we have a single agent with a single role |
| AutoGen | Requires GPT-4 by default; we committed to Anthropic-only |
| LlamaIndex Agents | Tool-calling focus; we deliberately have no tools for the LLM to call |

## Audit Trail

The absence of an agent framework can be independently verified:

```
grep -r "langchain\|langgraph\|crewai\|autogen\|llamaindex" pyproject.toml  # no results
grep -r "AgentExecutor\|Crew\|Graph\|Pipeline" simulation/runner.py  # no results
```

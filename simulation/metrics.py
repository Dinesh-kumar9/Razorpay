"""
Batch metrics computation — turns raw AuditRecord list into BatchMetrics.

Every metric maps directly to a judge criterion from the PRD.
The stopping_rule_violations count is the most critical: it MUST be zero.
If it is non-zero, the policy engine has a bug.

Dual-baseline reporting (added 2026-08-31):
  uplift_vs_blind_retry_pct      — vs single-attempt naive baseline (secondary)
  uplift_vs_naive_multi_retry_pct — vs 3-attempt realistic merchant cron (headline)
"""

from __future__ import annotations

from decimal import Decimal

from schemas.audit import AuditRecord, BatchMetrics
from schemas.decision import RecoveryAction
from schemas.transaction import HARD_STOP_CODES


def _compute_uplift(agent: Decimal, baseline: Decimal) -> float:
    """Compute % uplift; returns 0.0 if baseline is 0 (avoids division by zero)."""
    if baseline <= 0:
        return 0.0
    return float((agent - baseline) / baseline * 100)


def compute_metrics(
    records: list[AuditRecord],
    recovered_blind_retry: Decimal,
    recovered_naive_multi_retry: Decimal,
    seed: int = 42,
    recovered_constrained_multi_retry: Decimal | None = None,
    unconstrained_violations: dict[str, int] | int | None = None,
) -> BatchMetrics:
    """
    Compute all batch-level metrics from a list of AuditRecords.

    stopping_rule_violations: count of cases where was_overridden=False but
    the transaction had a hard-stop failure code — which would mean the policy
    engine failed to catch it. This MUST be zero.

    false_escalation_count: model (not policy engine) recommended escalation
    for a non-hard-stop code (unnecessary escalation of a recoverable transaction).

    Baseline comparisons computed:
      - vs constrained multi-retry baseline (primary headline benchmark)
      - vs single-attempt baseline (disclosed secondary figure)
      - vs unconstrained multi-retry baseline (illustrative comparison)
    """
    n = len(records)
    if n == 0:
        raise ValueError("Cannot compute metrics on an empty record list.")

    total_at_risk = sum((r.amount_inr for r in records), Decimal("0"))
    total_recovered_agent = sum((r.amount_recovered_inr for r in records), Decimal("0"))
    recovery_rate_agent = float(total_recovered_agent / total_at_risk * 100) if total_at_risk else 0.0

    # Independent uplift calculations against each baseline
    uplift_single = _compute_uplift(total_recovered_agent, recovered_blind_retry)
    uplift_multi = _compute_uplift(total_recovered_agent, recovered_naive_multi_retry)
    uplift_constrained = (
        _compute_uplift(total_recovered_agent, recovered_constrained_multi_retry)
        if recovered_constrained_multi_retry is not None
        else 0.0
    )
    violations_count = (
        sum(unconstrained_violations.values())
        if isinstance(unconstrained_violations, dict)
        else (unconstrained_violations or 0)
    )

    # Stopping-rule violations: hard-stop code processed WITHOUT escalation and NOT overridden
    # This would mean the policy engine missed a hard stop — must be 0.
    stopping_violations = sum(
        bool(
            r.failure_code in HARD_STOP_CODES
            and r.final_action != RecoveryAction.ESCALATE_TO_HUMAN
            and not r.was_overridden
        )
        for r in records
    )

    # Explanation coverage: MUST be 100% (fallback guarantees this)
    with_explanation = sum(1 for r in records if r.explanation is not None)
    explanation_pct = float(with_explanation / n * 100)

    # False escalations: model recommended escalation for non-hard-stop codes
    # (the policy engine is correct to escalate hard-stops; false = unnecessary escalation)
    false_escalations = sum(
        1
        for r in records
        if r.model_action == RecoveryAction.ESCALATE_TO_HUMAN
        and r.failure_code not in HARD_STOP_CODES
    )
    false_escalation_rate = float(false_escalations / n * 100)

    # Override tracking (genuine overrides: model_action != final_action)
    overrides = sum(bool(r.was_overridden) for r in records)
    override_rate = float(overrides / n * 100)

    # Guardrail rule fired tracking (all guardrail rule activations, statutory and internal)
    mandated = sum(bool(getattr(r, "rule_mandated", False) or r.guardrail_rule_id) for r in records)
    mandated_rate = float(mandated / n * 100)

    # LLM fallback tracking
    template_count = sum(
        bool(r.explanation and r.explanation.source == "template") for r in records
    )
    fallback_rate = float(template_count / n * 100)

    return BatchMetrics(
        random_seed=seed,
        total_transactions=n,
        total_at_risk_inr=total_at_risk,
        recovered_inr_agent=total_recovered_agent,
        recovery_rate_agent_pct=round(recovery_rate_agent, 2),
        recovered_inr_blind_retry=recovered_blind_retry,
        recovered_inr_naive_multi_retry=recovered_naive_multi_retry,
        recovered_inr_constrained_multi_retry=recovered_constrained_multi_retry or Decimal("0"),
        recovered_inr_never_retry=Decimal("0"),
        uplift_vs_blind_retry_pct=round(uplift_single, 4),
        uplift_vs_constrained_multi_retry_pct=round(uplift_constrained, 4),
        uplift_vs_naive_multi_retry_pct=round(uplift_multi, 4),
        unconstrained_violations_total=violations_count,
        stopping_rule_violations=stopping_violations,
        decisions_with_explanation_pct=round(explanation_pct, 2),
        false_escalation_count=false_escalations,
        false_escalation_rate_pct=round(false_escalation_rate, 2),
        override_count=overrides,
        override_rate_pct=round(override_rate, 2),
        rule_mandated_count=mandated,
        rule_mandated_rate_pct=round(mandated_rate, 2),
        llm_fallback_to_template_count=template_count,
        llm_fallback_rate_pct=round(fallback_rate, 2),
    )

"""Simulate router — live single-transaction demo route."""

from __future__ import annotations

import random
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter

from audit.logger import AuditLogger
from execution.executor import SimulatedExecutor
from llm_layer.client import LLMExplainer
from policy_engine.engine import PolicyEngine
from risk_model.model import RecoveryModel
from schemas.audit import AuditRecord
from schemas.transaction import FailedTransaction
from simulation.outcome_model import simulate_outcome

router = APIRouter(tags=["simulate"])

# Singletons for the simulate route (loaded once on first call)
_model: RecoveryModel | None = None
_engine: PolicyEngine = PolicyEngine()
_explainer: LLMExplainer | None = None
_executor: SimulatedExecutor = SimulatedExecutor()


def _get_model() -> RecoveryModel:
    global _model
    if _model is None:
        _model = RecoveryModel()
        _model.load_or_train()
    return _model


def _get_explainer() -> LLMExplainer:
    global _explainer
    if _explainer is None:
        _explainer = LLMExplainer()
    return _explainer


@router.post("/simulate/single", response_model=AuditRecord)
async def simulate_single(txn: FailedTransaction) -> AuditRecord:
    """
    Run a single transaction through the full pipeline and return the AuditRecord.
    Used in the live demo to show the pipeline working end-to-end.
    """
    model = _get_model()
    explainer = _get_explainer()
    audit = AuditLogger(Path("audit.db"))
    rng = random.Random()

    model_decision = model.predict(txn)
    policy_decision = _engine.evaluate(txn, model_decision)
    explanation = explainer.explain(
        policy_decision=policy_decision,
        shap_features=model_decision.shap_top_features,
        raw_gateway_error=txn.gateway_raw_error,
        amount_inr=txn.amount_inr,
        failure_code=txn.failure_code.value,
    )
    _executor.execute(txn, policy_decision)
    outcome = simulate_outcome(txn, policy_decision.final_action, rng)

    record = AuditRecord(
        txn_id=txn.txn_id,
        timestamp=datetime.now(tz=timezone.utc),
        amount_inr=txn.amount_inr,
        failure_code=txn.failure_code,
        payment_method=txn.payment_method,
        customer_id=txn.customer_id,
        merchant_id=txn.merchant_id,
        model_action=policy_decision.model_action,
        model_confidence=model_decision.confidence,
        final_action=policy_decision.final_action,
        was_overridden=policy_decision.was_overridden,
        override_reason=policy_decision.override_reason,
        guardrail_rule_id=policy_decision.guardrail_rule_id,
        retry_delay_minutes=policy_decision.retry_delay_minutes,
        explanation=explanation,
        simulated_outcome=outcome,
        amount_recovered_inr=outcome.amount_recovered_inr,
    )
    audit.log(record)
    return record

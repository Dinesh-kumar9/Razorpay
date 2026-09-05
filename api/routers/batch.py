"""Batch API routes — reads from the audit log."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from api.dependencies import get_audit_logger
from schemas.audit import AuditRecord, BatchMetrics

router = APIRouter(tags=["batch"])

# Canonical seed offsets — must match simulation/runner.py run_batch() and api/main.py.
_SIMULATION_SEED: int = 42
_BLIND_RETRY_SEED: int = _SIMULATION_SEED + 1000        # 1042
_MULTI_RETRY_SEED: int = _SIMULATION_SEED + 1500        # 1542
_CONSTRAINED_RETRY_SEED: int = _SIMULATION_SEED + 1750  # 1792


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": "0.1.0"}


@router.get("/batch/metrics", response_model=BatchMetrics)
async def get_batch_metrics() -> BatchMetrics:
    """Return aggregate batch metrics, computed fresh from the audit DB."""
    import random

    from ingestion.generator import generate_transactions
    from simulation.baselines import (
        run_blind_retry_baseline,
        run_naive_multi_retry_constrained,
        run_naive_multi_retry_with_violations,
    )
    from simulation.metrics import compute_metrics

    audit = get_audit_logger()
    record_count = audit.count()
    if record_count == 0:
        raise HTTPException(
            status_code=404,
            detail="No batch metrics found. Run `python -m simulation.runner` first.",
        )
    records = audit.get_all_records()
    txns = generate_transactions(n=record_count, random_seed=_SIMULATION_SEED)
    blind_rng = random.Random(_BLIND_RETRY_SEED)
    multi_rng = random.Random(_MULTI_RETRY_SEED)
    constrained_rng = random.Random(_CONSTRAINED_RETRY_SEED)
    recovered_blind = run_blind_retry_baseline(txns, blind_rng)
    recovered_multi, violations = run_naive_multi_retry_with_violations(txns, multi_rng)
    recovered_constrained = run_naive_multi_retry_constrained(txns, constrained_rng)
    return compute_metrics(
        records,
        recovered_blind,
        recovered_multi,
        seed=_SIMULATION_SEED,
        recovered_constrained_multi_retry=recovered_constrained,
        unconstrained_violations=violations,
    )


class TransactionListResponse(BaseModel):
    records: list[dict[str, object]]
    total: int
    page: int
    total_pages: int


@router.get("/batch/transactions", response_model=TransactionListResponse)
async def list_transactions(
    filter: str = Query(default="all", description="all | overridden | recovered | escalated | stopped"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> TransactionListResponse:
    """Return paginated list of AuditRecord summaries."""
    audit = get_audit_logger()

    filter_overridden: bool | None = None
    filter_recovered: bool | None = None
    filter_action: str | None = None

    if filter == "overridden":
        filter_overridden = True
    elif filter == "recovered":
        filter_recovered = True
    elif filter == "escalated":
        filter_action = "escalate_to_human"
    elif filter == "stopped":
        filter_action = "stop"

    rows, total = audit.get_summary_rows(
        filter_overridden=filter_overridden,
        filter_recovered=filter_recovered,
        filter_action=filter_action,
        page=page,
        page_size=page_size,
    )
    total_pages = max(1, (total + page_size - 1) // page_size)
    return TransactionListResponse(records=rows, total=total, page=page, total_pages=total_pages)


@router.get("/batch/transaction/{txn_id}", response_model=AuditRecord)
async def get_transaction(txn_id: str) -> AuditRecord:
    """Return the full AuditRecord for a specific transaction."""
    audit = get_audit_logger()
    record = audit.query_by_txn(txn_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Transaction {txn_id} not found.")
    return record


class ActionCount(BaseModel):
    action: str
    count: int


class FailureCodeCount(BaseModel):
    failure_code: str
    count: int


class BatchBreakdown(BaseModel):
    actions: list[ActionCount]
    failure_codes: list[FailureCodeCount]
    total: int


@router.get("/batch/breakdown", response_model=BatchBreakdown)
async def get_batch_breakdown() -> BatchBreakdown:
    """
    Return action distribution and failure code breakdown for chart rendering.
    Used by the dashboard donut chart — fetched client-side via JS.
    """
    from collections import Counter

    audit = get_audit_logger()
    record_count = audit.count()
    if record_count == 0:
        raise HTTPException(
            status_code=404,
            detail="No batch data found. Run `python -m simulation.runner` first.",
        )
    records = audit.get_all_records()
    action_counts = Counter(r.final_action.value for r in records)
    failure_counts = Counter(r.failure_code.value if hasattr(r.failure_code, 'value') else str(r.failure_code) for r in records)

    return BatchBreakdown(
        actions=[ActionCount(action=k, count=v) for k, v in sorted(action_counts.items(), key=lambda x: -x[1])],
        failure_codes=[FailureCodeCount(failure_code=k, count=v) for k, v in sorted(failure_counts.items(), key=lambda x: -x[1])],
        total=record_count,
    )


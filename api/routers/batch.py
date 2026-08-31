"""Batch API routes — reads from the audit log."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from api.dependencies import get_audit_logger
from schemas.audit import AuditRecord, BatchMetrics

router = APIRouter(tags=["batch"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": "0.1.0"}


@router.get("/batch/metrics", response_model=BatchMetrics)
async def get_batch_metrics() -> BatchMetrics:
    """Return aggregate batch metrics, computed fresh from the audit DB."""
    import random

    from ingestion.generator import generate_transactions
    from simulation.baselines import run_blind_retry_baseline, run_naive_multi_retry_baseline
    from simulation.metrics import compute_metrics

    audit = get_audit_logger()
    record_count = audit.count()
    if record_count == 0:
        raise HTTPException(
            status_code=404,
            detail="No batch metrics found. Run `python -m simulation.runner` first.",
        )
    records = audit.get_all_records()
    txns = generate_transactions(n=record_count, random_seed=42)
    blind_rng = random.Random(1042)
    multi_rng = random.Random(1542)
    recovered_blind = run_blind_retry_baseline(txns, blind_rng)
    recovered_multi = run_naive_multi_retry_baseline(txns, multi_rng)
    return compute_metrics(records, recovered_blind, recovered_multi)


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

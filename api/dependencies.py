"""API dependencies — shared singleton instances."""

from __future__ import annotations

from pathlib import Path

from audit.logger import AuditLogger
from schemas.audit import BatchMetrics

_audit_logger: AuditLogger | None = None
_demo_audit_logger: AuditLogger | None = None
_metrics_cache: BatchMetrics | None = None


def get_audit_logger() -> AuditLogger:
    global _audit_logger
    if _audit_logger is None:
        _audit_logger = AuditLogger(Path("audit.db"))
    return _audit_logger


def get_demo_audit_logger() -> AuditLogger:
    """Audit logger dedicated to live demo cases, preserving the canonical batch ledger."""
    global _demo_audit_logger
    if _demo_audit_logger is None:
        _demo_audit_logger = AuditLogger(Path("demo_audit.db"))
    return _demo_audit_logger


def get_metrics_cache() -> BatchMetrics | None:
    """Returns the cached BatchMetrics if available, else None."""
    return _metrics_cache


def set_metrics_cache(metrics: BatchMetrics) -> None:
    global _metrics_cache
    _metrics_cache = metrics

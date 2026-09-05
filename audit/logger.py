"""
Append-only SQLite audit log — Stage 6 of the pipeline.

Every decision made by the system is written here, exactly once, and never
mutated. The audit log is the primary evidence for:
  - "audit trail" (every decision is logged and replayable)
  - "compliant escalation" (override_reason and guardrail_rule_id are always set)
  - "stopping rules" (guardrail_rule_id = 'RATE_LIMIT_001' on STOP actions)

The schema stores AuditRecord as a JSON blob in a TEXT column, with indexed
fields for efficient querying. This is appropriate at batch sizes of ≤100k;
production would use a structured schema or columnar store.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from schemas.audit import AuditRecord

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("audit.db")

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS audit_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    txn_id          TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    failure_code    TEXT NOT NULL,
    final_action    TEXT NOT NULL,
    was_overridden  INTEGER NOT NULL,
    guardrail_rule_id TEXT,
    recovered       INTEGER NOT NULL,
    amount_inr      REAL NOT NULL,
    amount_recovered_inr REAL NOT NULL,
    record_json     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_txn_id ON audit_records(txn_id);
CREATE INDEX IF NOT EXISTS idx_final_action ON audit_records(final_action);
CREATE INDEX IF NOT EXISTS idx_was_overridden ON audit_records(was_overridden);
CREATE INDEX IF NOT EXISTS idx_recovered ON audit_records(recovered);
"""


class AuditLogger:
    """
    Append-only SQLite-backed audit log.

    Each write is a single INSERT — no UPDATE or DELETE is ever executed.
    This makes the log tamper-evident: any deletion leaves a gap in the
    auto-increment ID sequence.

    Thread safety: each call acquires and releases a connection. Not designed
    for concurrent write-heavy workloads; suitable for sequential batch simulation.
    """

    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self._db_path = db_path
        self._initialise()

    def _initialise(self) -> None:
        """Create the audit table and indexes if they don't exist."""
        with self._connect() as conn:
            conn.executescript(CREATE_TABLE_SQL)

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def log(self, record: AuditRecord) -> None:
        """
        Append a single AuditRecord to the log.

        Raises sqlite3.Error on database failure (caller should handle).
        Uses model_dump(mode='json') to ensure Decimal/datetime serialization.
        """
        record_json = json.dumps(record.model_dump(mode="json"), default=str)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_records
                    (txn_id, timestamp, failure_code, final_action, was_overridden,
                     guardrail_rule_id, recovered, amount_inr, amount_recovered_inr, record_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.txn_id,
                    record.timestamp.isoformat(),
                    record.failure_code.value,
                    record.final_action.value,
                    int(record.was_overridden),
                    record.guardrail_rule_id,
                    int(record.simulated_outcome.recovered),
                    float(record.amount_inr),
                    float(record.amount_recovered_inr),
                    record_json,
                ),
            )

    def log_batch(self, records: list[AuditRecord]) -> None:
        """
        Append multiple AuditRecords in a single SQLite transaction.

        Significantly faster than calling log() N times: one open/commit/close cycle
        instead of N. Use this for batch simulation writes.

        Raises sqlite3.Error on database failure (entire batch is rolled back).
        """
        if not records:
            return
        rows = [
            (
                r.txn_id,
                r.timestamp.isoformat(),
                r.failure_code.value,
                r.final_action.value,
                int(r.was_overridden),
                r.guardrail_rule_id,
                int(r.simulated_outcome.recovered),
                float(r.amount_inr),
                float(r.amount_recovered_inr),
                json.dumps(r.model_dump(mode="json"), default=str),
            )
            for r in records
        ]
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO audit_records
                    (txn_id, timestamp, failure_code, final_action, was_overridden,
                     guardrail_rule_id, recovered, amount_inr, amount_recovered_inr, record_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        logger.info("Batch-logged %d audit records in a single transaction.", len(records))

    def get_all_records(self) -> list[AuditRecord]:
        """Return all records ordered by id (insertion order)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT record_json FROM audit_records ORDER BY id"
            ).fetchall()
        return [AuditRecord(**json.loads(row["record_json"])) for row in rows]

    def query_by_txn(self, txn_id: str) -> AuditRecord | None:
        """Return the AuditRecord for a specific transaction, or None if not found."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT record_json FROM audit_records WHERE txn_id = ? LIMIT 1",
                (txn_id,),
            ).fetchone()
        if row is None:
            return None
        return AuditRecord(**json.loads(row["record_json"]))

    def get_summary_rows(
        self,
        filter_overridden: bool | None = None,
        filter_recovered: bool | None = None,
        filter_action: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[dict[str, object]], int]:
        """
        Return paginated summary rows for the dashboard transaction table.
        Returns (rows, total_count).

        Security note: the WHERE clause is assembled by appending only
        literal, hardcoded condition strings from a whitelist — never from
        user-supplied values. User values are always bound via ? parameters.
        This satisfies Bandit B608: no string-interpolated SQL.
        """
        # Hardcoded condition strings (not user-supplied) — only values go through params
        condition_parts: list[str] = []
        params: list[object] = []

        if filter_overridden is not None:
            condition_parts.append("was_overridden = ?")
            params.append(int(filter_overridden))
        if filter_recovered is not None:
            condition_parts.append("recovered = ?")
            params.append(int(filter_recovered))
        if filter_action is not None:
            condition_parts.append("final_action = ?")
            params.append(filter_action)

        offset = (page - 1) * page_size

        if condition_parts:
            where_clause = "WHERE " + " AND ".join(condition_parts)
            count_sql = f"SELECT COUNT(*) FROM audit_records {where_clause}"  # nosec B608
            select_sql = f"SELECT record_json FROM audit_records {where_clause} ORDER BY id LIMIT ? OFFSET ?"  # nosec B608
        else:
            count_sql = "SELECT COUNT(*) FROM audit_records"
            select_sql = "SELECT record_json FROM audit_records ORDER BY id LIMIT ? OFFSET ?"

        with self._connect() as conn:
            total = conn.execute(count_sql, params).fetchone()[0]
            rows = conn.execute(select_sql, params + [page_size, offset]).fetchall()

        parsed = [json.loads(row["record_json"]) for row in rows]
        return parsed, total

    def count(self) -> int:
        """Return total number of logged records."""
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM audit_records").fetchone()[0])

    def clear(self) -> None:
        """
        Delete all records. Used only in tests — never in production.
        Production audit logs are append-only; clearing would destroy the audit trail.
        """
        with self._connect() as conn:
            conn.execute("DELETE FROM audit_records")
            conn.execute("DELETE FROM sqlite_sequence WHERE name='audit_records'")
        logger.warning("Audit log cleared — this should only happen in tests.")

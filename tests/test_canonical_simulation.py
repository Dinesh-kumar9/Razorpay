"""
Comprehensive test suite verifying canonical simulation consistency,
determinism, baseline uplift calculations, and dashboard alignment.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.dependencies import get_audit_logger
from api.main import app
from schemas.decision import RecoveryAction
from schemas.transaction import HARD_STOP_CODES
from simulation.runner import run_batch


class TestCanonicalSimulation:
    """Tests establishing the authoritative canonical experiment and database integrity."""

    def test_canonical_batch_size_in_audit_db_is_exactly_5000(self) -> None:
        """The canonical batch audit ledger must contain exactly 5,000 transactions."""
        audit = get_audit_logger()
        count = audit.count()
        assert count == 5000, f"Expected exactly 5,000 records, found {count}"

    def test_canonical_metrics_values(self) -> None:
        """Verify the exact canonical numbers produced by the authoritative simulation."""
        audit = get_audit_logger()
        records = audit.get_all_records()
        assert len(records) == 5000

        total_recovered = sum((r.amount_recovered_inr for r in records), Decimal("0"))
        # Meridian recovered revenue: Rs. 9,525,989
        assert round(total_recovered, 2) == Decimal("9525989.19")

        # Overrides: exactly 2,284 (45.68%)
        overrides = sum(bool(r.was_overridden) for r in records)
        assert overrides == 2284
        assert round(overrides / 5000 * 100, 2) == 45.68

        # Guardrail rules fired: exactly 3,809 (76.18%)
        rules_fired = sum(bool(getattr(r, "rule_mandated", False) or r.guardrail_rule_id) for r in records)
        assert rules_fired == 3809
        assert round(rules_fired / 5000 * 100, 2) == 76.18

        # Stopping rule violations must be exactly 0
        stopping_violations = sum(
            bool(
                r.failure_code in HARD_STOP_CODES
                and r.final_action != RecoveryAction.ESCALATE_TO_HUMAN
                and not r.was_overridden
            )
            for r in records
        )
        assert stopping_violations == 0

        # Explanation coverage: exactly 100.0%
        explanations = sum(1 for r in records if r.explanation is not None)
        assert explanations == 5000

    def test_simulation_determinism_small_batch(self, tmp_path: Path) -> None:
        """Running the simulation twice with the same seed produces identical results."""
        db1 = tmp_path / "sim1.db"
        db2 = tmp_path / "sim2.db"

        m1 = run_batch(n=100, seed=42, db_path=db1)
        m2 = run_batch(n=100, seed=42, db_path=db2)

        assert m1.total_transactions == m2.total_transactions == 100
        assert m1.total_at_risk_inr == m2.total_at_risk_inr
        assert m1.recovered_inr_agent == m2.recovered_inr_agent
        assert m1.recovered_inr_blind_retry == m2.recovered_inr_blind_retry
        assert m1.recovered_inr_constrained_multi_retry == m2.recovered_inr_constrained_multi_retry
        assert m1.recovered_inr_naive_multi_retry == m2.recovered_inr_naive_multi_retry
        assert m1.uplift_vs_blind_retry_pct == m2.uplift_vs_blind_retry_pct
        assert m1.uplift_vs_constrained_multi_retry_pct == m2.uplift_vs_constrained_multi_retry_pct
        assert m1.override_count == m2.override_count
        assert m1.rule_mandated_count == m2.rule_mandated_count

    def test_dashboard_api_matches_canonical_results(self) -> None:
        """Verify that GET /api/batch/metrics returns the canonical verified metrics."""
        client = TestClient(app)
        resp = client.get("/api/batch/metrics")
        assert resp.status_code == 200
        data = resp.json()

        assert data["total_transactions"] == 5000
        assert float(data["recovered_inr_agent"]) == 9525989.19
        assert float(data["recovered_inr_blind_retry"]) == 1853478.79
        assert float(data["recovered_inr_constrained_multi_retry"]) == 5004245.05
        assert float(data["recovered_inr_naive_multi_retry"]) == 11702972.40

        # Uplift vs single retry (~ +414.0%)
        assert data["uplift_vs_blind_retry_pct"] == pytest.approx(413.95, rel=1e-2)
        # Uplift vs constrained multi-retry (~ +90.4%)
        assert data["uplift_vs_constrained_multi_retry_pct"] == pytest.approx(90.36, rel=1e-2)
        # Uplift vs unconstrained multi-retry (~ -18.6%)
        assert data["uplift_vs_naive_multi_retry_pct"] == pytest.approx(-18.60, rel=1e-2)

        assert data["stopping_rule_violations"] == 0
        assert data["override_count"] == 2284
        assert data["rule_mandated_count"] == 3809
        assert data["decisions_with_explanation_pct"] == 100.0

    def test_dashboard_html_renders_honest_labels_and_correct_uplifts(self) -> None:
        """Verify the HTML dashboard contains honest labels and headline benchmark."""
        client = TestClient(app)
        resp = client.get("/")
        assert resp.status_code == 200
        html = resp.text

        # Terminology and counts
        assert "5,000 synthetic transactions" in html
        assert "Simulated Recovered Revenue" in html
        assert "Uplift vs Constrained Multi-Retry" in html
        assert "Fair policy-gated baseline" in html
        assert "Uplift vs Single Retry" in html
        assert "Model Overrides" in html
        assert "Guardrail Rules Fired" in html
        assert "Configured Stopping-Rule Violations" in html

        # Key values
        assert "9,525,989" in html
        assert "+90.4%" in html
        assert "+414.0%" in html
        assert "5,004,245" in html

    def test_simulate_single_does_not_pollute_canonical_audit_db(self) -> None:
        """Verify that calling /api/simulate/single does not increment audit.db count."""
        audit = get_audit_logger()
        count_before = audit.count()
        assert count_before == 5000

        client = TestClient(app)
        payload = {
            "txn_id": "TXN-DEMO-CHECK-001",
            "merchant_id": "merch_test",
            "customer_id": "cust_test",
            "amount_inr": 25000.0,
            "payment_method": "upi",
            "failure_code": "insufficient_funds",
            "retry_count_so_far": 0,
            "time_of_failure": "2026-09-04T12:00:00Z",
            "gateway_raw_error": "Insufficient funds",
            "customer_contact_count_24h": 0,
            "last_contact_time": None,
            "is_subscription": False,
        }
        resp = client.post("/api/simulate/single", json=payload)
        assert resp.status_code == 200

        count_after = audit.count()
        assert count_after == 5000, f"Expected 5,000, but got {count_after}. simulate_single leaked into audit.db!"

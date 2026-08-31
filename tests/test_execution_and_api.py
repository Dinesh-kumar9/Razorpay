"""
Unit tests for execution layer, ingestion, simulation baselines, and FastAPI endpoints.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from decimal import Decimal
from fastapi.testclient import TestClient

from api.main import app
from execution.executor import SimulatedExecutor
from ingestion.generator import generate_transactions
from schemas.decision import PolicyDecision, RecoveryAction
from schemas.transaction import FailedTransaction, FailureCode, PaymentMethod
from simulation.baselines import (
    run_blind_retry_baseline,
    run_naive_multi_retry_baseline,
    run_never_retry_baseline,
)
from simulation.outcome_model import simulate_outcome


class TestBaselinesAndOutcomes:
    def test_never_retry_baseline_zero(self):
        txns = generate_transactions(n=5, random_seed=42)
        recovered = run_never_retry_baseline(txns)
        assert recovered == Decimal("0")

    def test_single_and_multi_retry_baselines(self):
        txns = generate_transactions(n=50, random_seed=42)
        rng1 = random.Random(42)
        rng2 = random.Random(42)
        rec_single = run_blind_retry_baseline(txns, rng1)
        rec_multi = run_naive_multi_retry_baseline(txns, rng2)
        assert isinstance(rec_single, Decimal)
        assert isinstance(rec_multi, Decimal)
        assert rec_multi >= rec_single

    def test_simulate_recovery_outcome_hard_stop(self):
        txns = generate_transactions(n=10, random_seed=42)
        hard_stop_txn = [t for t in txns if t.failure_code == FailureCode.FRAUD_FLAG]
        if hard_stop_txn:
            outcome = simulate_outcome(
                hard_stop_txn[0],
                RecoveryAction.RETRY_NOW,
                rng=random.Random(42),
            )
            assert outcome.recovered is False
            assert outcome.amount_recovered_inr == Decimal("0")


class TestSimulatedExecutor:
    def test_executor_dispatch(self):
        executor = SimulatedExecutor()
        txn = generate_transactions(n=1, random_seed=42)[0]
        policy_decision = PolicyDecision(
            txn_id=txn.txn_id,
            final_action=RecoveryAction.RETRY_DELAYED,
            model_action=RecoveryAction.RETRY_DELAYED,
            was_overridden=False,
            retry_delay_minutes=1440,
        )
        descriptor = executor.execute(txn, policy_decision)
        assert isinstance(descriptor, str)
        assert "SIMULATED" in descriptor
        assert "retry_delayed" in descriptor.lower() or "resend" in descriptor.lower() or "1440" in descriptor


class TestFastAPIEndpoints:
    def test_health_check(self):
        client = TestClient(app)
        response = client.get("/api/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"

    def test_batch_metrics_endpoint(self):
        client = TestClient(app)
        response = client.get("/api/batch/metrics")
        assert response.status_code == 200
        data = response.json()
        assert "total_transactions" in data
        assert "total_at_risk_inr" in data

    def test_simulate_single_endpoint(self):
        client = TestClient(app)
        payload = {
            "txn_id": "TXN-SIM-TEST-001",
            "merchant_id": "merch_fastapi",
            "customer_id": "cust_fastapi",
            "amount_inr": 2500.0,
            "payment_method": "upi",
            "failure_code": "insufficient_funds",
            "retry_count_so_far": 0,
            "time_of_failure": datetime.now(timezone.utc).isoformat(),
            "gateway_raw_error": "Insufficient funds in bank account",
            "customer_contact_count_24h": 0,
            "last_contact_time": None,
            "is_subscription": False,
        }
        response = client.post("/api/simulate/single", json=payload)
        assert response.status_code == 200
        res = response.json()
        assert res["txn_id"] == "TXN-SIM-TEST-001"
        assert "final_action" in res
        assert "explanation" in res
        assert "simulated_outcome" in res

    def test_htmx_and_html_views(self):
        client = TestClient(app)
        r_index = client.get("/")
        assert r_index.status_code == 200
        assert "Revenue Recovery" in r_index.text

        r_txns = client.get("/transactions")
        assert r_txns.status_code == 200

        r_htmx = client.get("/htmx/transactions?filter=all&page=1")
        assert r_htmx.status_code == 200


class TestSimulationRunner:
    def test_run_batch_pipeline(self, tmp_path):
        from simulation.runner import run_batch
        db_path = tmp_path / "sim_audit.db"
        metrics = run_batch(n=10, seed=42, db_path=db_path)
        assert metrics.total_transactions == 10
        assert metrics.total_at_risk_inr > 0
        assert metrics.stopping_rule_violations == 0
        assert metrics.decisions_with_explanation_pct == 100.0

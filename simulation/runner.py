"""
Batch simulation runner — orchestrates the full 8-stage pipeline.

Run this module directly to execute the batch simulation:
    python -m simulation.runner

Or via Docker Compose:
    docker compose run simulation

The simulation is fully reproducible: running with the same seed always
produces identical metrics. This is a hard requirement — the README metrics
table must be regenerable on any machine.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from rich import box
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.table import Table

from audit.logger import AuditLogger
from execution.executor import SimulatedExecutor
from ingestion.generator import generate_transactions
from llm_layer.client import LLMExplainer
from policy_engine.engine import PolicyEngine
from risk_model.model import RecoveryModel
from schemas.audit import AuditRecord, BatchMetrics
from schemas.transaction import FailedTransaction
from simulation.baselines import (
    run_blind_retry_baseline,
    run_naive_multi_retry_baseline,
    run_never_retry_baseline,
)
from simulation.metrics import compute_metrics
from simulation.outcome_model import simulate_outcome

logger = logging.getLogger(__name__)
console = Console()

DEFAULT_BATCH_SIZE = 5_000
DEFAULT_SEED = 42
DEFAULT_DB_PATH = Path("audit.db")


def run_single(
    txn: FailedTransaction,
    model: RecoveryModel,
    engine: PolicyEngine,
    explainer: LLMExplainer,
    executor: SimulatedExecutor,
    audit_log: AuditLogger,
    rng: random.Random,
) -> AuditRecord:
    """
    Process a single transaction through the full pipeline.

    Pipeline stages executed:
      1. Feature extraction + model scoring → ModelDecision
      2. Policy engine evaluation → PolicyDecision
      3. LLM explanation generation → LLMExplanation
      4. Simulated execution → API descriptor
      5. Outcome simulation → SimulatedOutcome
      6. Audit log write → AuditRecord

    This function is the one we show in the pitch:
    "Here is every step, in order, for a single transaction."
    """
    # Stage 2: Risk model
    model_decision = model.predict(txn)

    # Stage 3: Policy engine (has final authority)
    policy_decision = engine.evaluate(txn, model_decision)

    # Stage 4: LLM explanation (advisory only — cannot change the action)
    explanation = explainer.explain(
        policy_decision=policy_decision,
        shap_features=model_decision.shap_top_features,
        raw_gateway_error=txn.gateway_raw_error,
        amount_inr=txn.amount_inr,
        failure_code=txn.failure_code.value,
    )

    # Stage 5: Simulated execution
    executor.execute(txn, policy_decision)

    # Stage 6: Outcome simulation
    outcome = simulate_outcome(txn, policy_decision.final_action, rng)

    # Stage 6: Audit log
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
    audit_log.log(record)
    return record


def run_batch(
    n: int = DEFAULT_BATCH_SIZE,
    seed: int = DEFAULT_SEED,
    db_path: Path = DEFAULT_DB_PATH,
) -> BatchMetrics:
    """
    Run the full batch simulation over n synthetic failed transactions.

    Steps:
      0. Load/train model (cached at data/models/recovery_model.pkl)
      1. Generate n synthetic transactions (seeded)
      2. Run baselines over the same transactions (same RNG fork)
      3. Run agent pipeline for each transaction
      4. Compute and return BatchMetrics

    Reproducibility: all randomness flows through a single seeded RNG.
    Running with seed=42 always produces the exact metrics in README.md.
    """
    console.rule("[bold cyan]Project Meridian — Batch Simulation[/bold cyan]")
    console.print(f"  Batch size: {n:,}  |  Seed: {seed}  |  DB: {db_path}\n")

    # Initialise components
    model = RecoveryModel()
    engine = PolicyEngine()
    explainer = LLMExplainer()
    executor = SimulatedExecutor()
    audit_log = AuditLogger(db_path)

    # Load or train the model
    with console.status("[bold]Loading / training model...[/bold]"):
        model.load_or_train()
    console.print("  [green]OK[/green] Model ready")

    # Generate synthetic transactions
    with console.status("[bold]Generating synthetic transactions...[/bold]"):
        transactions = generate_transactions(n=n, random_seed=seed)
    console.print(f"  [green]OK[/green] Generated {len(transactions):,} transactions")

    # Fork the RNG: each baseline and agent get separate streams from same seed
    # so all are reproducible and independently comparable
    single_retry_rng = random.Random(seed + 1000)
    multi_retry_rng = random.Random(seed + 1500)
    agent_rng = random.Random(seed + 2000)

    # Run baselines
    with console.status("[bold]Running baselines...[/bold]"):
        recovered_blind = run_blind_retry_baseline(transactions, single_retry_rng)
        recovered_multi = run_naive_multi_retry_baseline(transactions, multi_retry_rng)
        _ = run_never_retry_baseline(transactions)
    console.print(
        f"  [green]OK[/green] Baselines: "
        f"single_retry=Rs.{recovered_blind:,.0f} | "
        f"multi_retry=Rs.{recovered_multi:,.0f} | "
        f"never_retry=Rs.0"
    )

    # Run agent pipeline
    records: list[AuditRecord] = []
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold cyan]Processing transactions..."),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("", total=n)
        for txn in transactions:
            record = run_single(
                txn=txn,
                model=model,
                engine=engine,
                explainer=explainer,
                executor=executor,
                audit_log=audit_log,
                rng=agent_rng,
            )
            records.append(record)
            progress.advance(task)

    # Compute and display metrics
    metrics = compute_metrics(records, recovered_blind, recovered_multi, seed=seed)
    _print_metrics_table(metrics)

    return metrics


def _print_metrics_table(m: BatchMetrics) -> None:
    """Display the batch metrics in a rich table for the terminal."""
    console.print()
    console.rule("[bold green]Batch Results[/bold green]")

    table = Table(box=box.ROUNDED, show_header=True, header_style="bold magenta")
    table.add_column("Metric", style="cyan", no_wrap=True)
    table.add_column("Value", justify="right")
    table.add_column("Target", justify="center", style="dim")
    table.add_column("Status", justify="center")

    def status(ok: bool) -> str:
        return "[green]PASS[/green]" if ok else "[red]FAIL[/red]"

    table.add_row("Total transactions", f"{m.total_transactions:,}", "-", "")
    table.add_row("Total at-risk", f"Rs.{m.total_at_risk_inr:,.0f}", "-", "")
    table.add_row("", "", "", "")
    table.add_row("Agent recovered", f"Rs.{m.recovered_inr_agent:,.0f}", "-", "")
    table.add_row(
        "Single-retry baseline",
        f"Rs.{m.recovered_inr_blind_retry:,.0f}",
        "-",
        "",
    )
    table.add_row(
        "Multi-retry baseline (realistic)",
        f"Rs.{m.recovered_inr_naive_multi_retry:,.0f}",
        "-",
        "",
    )
    table.add_row("Never-retry (floor)", "Rs.0", "-", "")
    table.add_row("", "", "", "")
    table.add_row(
        "Uplift vs single-attempt baseline",
        f"+{m.uplift_vs_blind_retry_pct:.1f}%",
        "disclosed",
        "[dim]secondary[/dim]",
    )
    table.add_row(
        "Uplift vs realistic multi-retry",
        f"+{m.uplift_vs_naive_multi_retry_pct:.1f}%",
        ">=20%",
        status(m.uplift_vs_naive_multi_retry_pct >= 20),
    )
    table.add_row(
        "Stopping-rule violations",
        str(m.stopping_rule_violations),
        "0",
        status(m.stopping_rule_violations == 0),
    )
    table.add_row(
        "Decisions with explanation",
        f"{m.decisions_with_explanation_pct:.1f}%",
        "100%",
        status(m.decisions_with_explanation_pct >= 100.0),
    )
    table.add_row(
        "False-escalation count",
        f"{m.false_escalation_count} ({m.false_escalation_rate_pct:.1f}%)",
        "reported",
        "[yellow]honest[/yellow]",
    )
    table.add_row(
        "Override count",
        f"{m.override_count} ({m.override_rate_pct:.1f}%)",
        "reported",
        "[yellow]transparent[/yellow]",
    )
    table.add_row(
        "LLM fallback to template",
        f"{m.llm_fallback_to_template_count} ({m.llm_fallback_rate_pct:.1f}%)",
        "reported",
        "[yellow]honest[/yellow]",
    )

    console.print(table)
    console.print(f"\n  [dim]Random seed: {m.random_seed} | Reproducible: run again to verify[/dim]\n")


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.WARNING)

    n_arg = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_BATCH_SIZE
    seed_arg = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_SEED

    run_batch(n=n_arg, seed=seed_arg)

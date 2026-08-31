"""
Simulated executor — Stage 5 of the pipeline.

In this build, execution is simulated. No real API calls are made to Razorpay.
Each action is logged as "would call Razorpay API X with params Y".

The outcome is then sampled from the documented recovery rate for the
(failure_code, final_action) pair — see simulation/outcome_model.py.

In production, this module would be replaced with real Razorpay API calls:
  retry_now        → POST /v1/payments/{id}/retry
  retry_delayed    → POST /v1/payment_links/{id}/resend (scheduled)
  nudge_alt_method → POST /v1/payment_links/{id}/resend (new link)
  escalate_to_human → POST /v1/disputes/{id}/escalate or internal ticketing
"""

from __future__ import annotations

import logging

from schemas.decision import PolicyDecision, RecoveryAction
from schemas.transaction import FailedTransaction

logger = logging.getLogger(__name__)


class SimulatedExecutor:
    """
    Simulated execution layer — logs the API call that would be made in production.

    The simulation's outcome model (not this class) determines whether the
    transaction is recovered. This class is responsible only for logging the
    action and returning the API descriptor string for the audit trail.
    """

    def execute(self, txn: FailedTransaction, decision: PolicyDecision) -> str:
        """
        Log the simulated execution and return the API descriptor string.

        Returns a string like "SIMULATED: POST /v1/payments/TXN-00001/retry"
        that is stored in the audit log to make the pipeline legible.
        """
        action = decision.final_action
        descriptor = self._get_api_descriptor(txn, action, decision.retry_delay_minutes)
        logger.debug("Executing %s for txn %s: %s", action.value, txn.txn_id, descriptor)
        return descriptor

    def _get_api_descriptor(
        self,
        txn: FailedTransaction,
        action: RecoveryAction,
        delay_minutes: int | None,
    ) -> str:
        """Maps a recovery action to its simulated Razorpay API call."""
        base = f"SIMULATED [{txn.txn_id} · ₹{txn.amount_inr}]"

        if action == RecoveryAction.RETRY_NOW:
            return f"{base}: POST /v1/payments/retry (immediate)"

        if action == RecoveryAction.RETRY_DELAYED:
            delay_str = f"{delay_minutes}min delay" if delay_minutes else "default delay"
            return f"{base}: POST /v1/payments/retry (scheduled, {delay_str})"

        if action == RecoveryAction.NUDGE_ALT_METHOD:
            return f"{base}: POST /v1/payment_links/resend (alt-method nudge to customer)"

        if action == RecoveryAction.ESCALATE_TO_HUMAN:
            return f"{base}: POST /v1/disputes/escalate (routed to human queue)"

        if action == RecoveryAction.STOP:
            return f"{base}: NO_OP (all recovery attempts exhausted)"

        return f"{base}: UNKNOWN_ACTION ({action.value})"  # fallback for type safety

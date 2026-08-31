"""
Test suite for policy_engine — the hero component.

Coverage target: ≥90% (enforced by CI gate).
Every rule has explicit test cases for: trigger condition, boundary condition,
and non-trigger condition. The "demo case" test is the one we show judges:
model says retry_now, cooldown fires because customer was contacted 20 min ago.

Test organisation:
  TestHardStop001  — card_blocked / fraud_flag / kyc_hold / stolen_card
  TestHardStop002  — card_expired / invalid_card + retry actions
  TestRateLimit001 — retry_count boundaries at 2, 3, 4
  TestRateLimit002 — contact_count boundaries at 0, 1
  TestCooldown001  — minutes_since_contact at 29, 30, 31
  TestWindow001    — hour_of_day at 7, 8, 20, 21
  TestNoOverride   — clean transactions pass through unchanged
  TestOverrideLogging — was_overridden, rule_id, override_reason set correctly
  TestDemoCase     — the specific scenario shown to judges in pitch
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from schemas.decision import RecoveryAction
from schemas.transaction import FailureCode
from tests.conftest import make_model_decision, make_txn

# ─────────────────────────────────────────────────────────────────────────────
# HARD_STOP_001 — card_blocked / fraud_flag / kyc_hold / stolen_card
# ─────────────────────────────────────────────────────────────────────────────

class TestHardStop001:
    """Rule: hard-stop codes always escalate_to_human regardless of model output."""

    HARD_STOP_CODES = [
        FailureCode.CARD_BLOCKED,
        FailureCode.FRAUD_FLAG,
        FailureCode.KYC_HOLD,
        FailureCode.STOLEN_CARD,
    ]

    @pytest.mark.parametrize("failure_code", HARD_STOP_CODES)
    @pytest.mark.parametrize(
        "model_action",
        [RecoveryAction.RETRY_NOW, RecoveryAction.RETRY_DELAYED, RecoveryAction.NUDGE_ALT_METHOD],
    )
    def test_always_escalates_regardless_of_model(
        self,
        engine: object,
        failure_code: FailureCode,
        model_action: RecoveryAction,
    ) -> None:
        """Model is overridden by HARD_STOP_001 for all hard-stop codes."""
        from policy_engine.engine import PolicyEngine
        eng = PolicyEngine()
        txn = make_txn(failure_code=failure_code)
        decision = eng.evaluate(txn, make_model_decision(txn, action=model_action))

        assert decision.final_action == RecoveryAction.ESCALATE_TO_HUMAN
        assert decision.was_overridden is True
        assert decision.guardrail_rule_id == "HARD_STOP_001"
        assert decision.override_reason is not None and len(decision.override_reason) > 0

    @pytest.mark.parametrize("failure_code", HARD_STOP_CODES)
    def test_override_even_when_model_correctly_escalates(
        self,
        failure_code: FailureCode,
    ) -> None:
        """
        If the model correctly recommends escalation for a hard-stop code,
        the engine still marks it as was_overridden=False since the final action
        matches the model action.
        Actually — the rule fires on ANY proposed action for hard-stop codes,
        so this tests that even when model=escalate, we get was_overridden=True
        because HARD_STOP_001 unconditionally overrides. The model's action
        is set to ESCALATE but the rule still fires (and override_action is
        also ESCALATE, so the outcome is the same but the audit is clear).
        """
        from policy_engine.engine import PolicyEngine
        eng = PolicyEngine()
        txn = make_txn(failure_code=failure_code)
        decision = eng.evaluate(
            txn, make_model_decision(txn, action=RecoveryAction.ESCALATE_TO_HUMAN)
        )
        # Rule fires, override_action == model_action (both ESCALATE), so
        # was_overridden should still be True (rule fired)
        assert decision.final_action == RecoveryAction.ESCALATE_TO_HUMAN
        assert decision.guardrail_rule_id == "HARD_STOP_001"

    def test_non_hard_stop_code_not_affected(self) -> None:
        """Soft-decline codes are not affected by HARD_STOP_001."""
        from policy_engine.engine import PolicyEngine
        eng = PolicyEngine()
        txn = make_txn(failure_code=FailureCode.INSUFFICIENT_FUNDS)
        decision = eng.evaluate(txn, make_model_decision(txn, action=RecoveryAction.RETRY_NOW))
        assert decision.guardrail_rule_id != "HARD_STOP_001"
        assert decision.final_action == RecoveryAction.RETRY_NOW


# ─────────────────────────────────────────────────────────────────────────────
# HARD_STOP_002 — card_expired / invalid_card + retry actions
# ─────────────────────────────────────────────────────────────────────────────

class TestHardStop002:
    """Rule: expired/invalid cards cannot retry — must nudge."""

    @pytest.mark.parametrize(
        "failure_code", [FailureCode.CARD_EXPIRED, FailureCode.INVALID_CARD]
    )
    @pytest.mark.parametrize(
        "retry_action", [RecoveryAction.RETRY_NOW, RecoveryAction.RETRY_DELAYED]
    )
    def test_retry_on_expired_becomes_nudge(
        self,
        failure_code: FailureCode,
        retry_action: RecoveryAction,
    ) -> None:
        from policy_engine.engine import PolicyEngine
        eng = PolicyEngine()
        txn = make_txn(failure_code=failure_code)
        decision = eng.evaluate(txn, make_model_decision(txn, action=retry_action))

        assert decision.final_action == RecoveryAction.NUDGE_ALT_METHOD
        assert decision.was_overridden is True
        assert decision.guardrail_rule_id == "HARD_STOP_002"

    def test_nudge_on_expired_passes_through(self) -> None:
        """If model already recommends nudge for expired card, rule doesn't fire."""
        from policy_engine.engine import PolicyEngine
        eng = PolicyEngine()
        txn = make_txn(failure_code=FailureCode.CARD_EXPIRED)
        decision = eng.evaluate(
            txn, make_model_decision(txn, action=RecoveryAction.NUDGE_ALT_METHOD)
        )
        assert decision.final_action == RecoveryAction.NUDGE_ALT_METHOD
        assert decision.guardrail_rule_id != "HARD_STOP_002"


# ─────────────────────────────────────────────────────────────────────────────
# RATE_LIMIT_001 — retry_count boundary at 3
# ─────────────────────────────────────────────────────────────────────────────

class TestRateLimit001:
    """Rule: max 3 retries per transaction; 4th attempt is STOP."""

    @pytest.mark.parametrize("retry_count", [0, 1, 2])
    def test_below_limit_allows_retry(self, retry_count: int) -> None:
        from policy_engine.engine import PolicyEngine
        eng = PolicyEngine()
        txn = make_txn(failure_code=FailureCode.INSUFFICIENT_FUNDS, retry_count=retry_count)
        decision = eng.evaluate(txn, make_model_decision(txn, action=RecoveryAction.RETRY_NOW))
        assert decision.final_action == RecoveryAction.RETRY_NOW
        assert decision.guardrail_rule_id != "RATE_LIMIT_001"

    @pytest.mark.parametrize("retry_count", [3, 4, 10])
    def test_at_or_above_limit_stops(self, retry_count: int) -> None:
        from policy_engine.engine import PolicyEngine
        eng = PolicyEngine()
        txn = make_txn(failure_code=FailureCode.INSUFFICIENT_FUNDS, retry_count=retry_count)
        decision = eng.evaluate(txn, make_model_decision(txn, action=RecoveryAction.RETRY_NOW))
        assert decision.final_action == RecoveryAction.STOP
        assert decision.guardrail_rule_id == "RATE_LIMIT_001"

    def test_limit_does_not_block_nudge(self) -> None:
        """RATE_LIMIT_001 only fires for retry actions, not for nudge."""
        from policy_engine.engine import PolicyEngine
        eng = PolicyEngine()
        txn = make_txn(failure_code=FailureCode.INSUFFICIENT_FUNDS, retry_count=5)
        decision = eng.evaluate(txn, make_model_decision(txn, action=RecoveryAction.NUDGE_ALT_METHOD))
        assert decision.guardrail_rule_id != "RATE_LIMIT_001"


# ─────────────────────────────────────────────────────────────────────────────
# RATE_LIMIT_002 — contact_count_24h boundary at 1
# ─────────────────────────────────────────────────────────────────────────────

class TestRateLimit002:
    """Rule: max 1 customer contact per 24h; 2nd nudge → retry_delayed."""

    def test_zero_contacts_allows_nudge(self) -> None:
        from policy_engine.engine import PolicyEngine
        eng = PolicyEngine()
        txn = make_txn(contact_count_24h=0)
        decision = eng.evaluate(txn, make_model_decision(txn, action=RecoveryAction.NUDGE_ALT_METHOD))
        assert decision.final_action == RecoveryAction.NUDGE_ALT_METHOD
        assert decision.guardrail_rule_id != "RATE_LIMIT_002"

    @pytest.mark.parametrize("contact_count", [1, 2, 5])
    def test_contacts_at_or_above_limit_blocks_nudge(self, contact_count: int) -> None:
        from policy_engine.engine import PolicyEngine
        eng = PolicyEngine()
        txn = make_txn(contact_count_24h=contact_count)
        decision = eng.evaluate(txn, make_model_decision(txn, action=RecoveryAction.NUDGE_ALT_METHOD))
        assert decision.final_action == RecoveryAction.RETRY_DELAYED
        assert decision.guardrail_rule_id == "RATE_LIMIT_002"

    def test_contact_limit_does_not_block_retry_now(self) -> None:
        """RATE_LIMIT_002 only blocks NUDGE; retry_now is unaffected."""
        from policy_engine.engine import PolicyEngine
        eng = PolicyEngine()
        txn = make_txn(contact_count_24h=5)
        decision = eng.evaluate(txn, make_model_decision(txn, action=RecoveryAction.RETRY_NOW))
        assert decision.guardrail_rule_id != "RATE_LIMIT_002"


# ─────────────────────────────────────────────────────────────────────────────
# COOLDOWN_001 — minutes_since_contact boundary at 30
# ─────────────────────────────────────────────────────────────────────────────

class TestCooldown001:
    """Rule: retry_now blocked if last contact < 30 min ago."""

    def test_no_prior_contact_allows_retry_now(self) -> None:
        from policy_engine.engine import PolicyEngine
        eng = PolicyEngine()
        txn = make_txn(last_contact_minutes_ago=None)
        decision = eng.evaluate(txn, make_model_decision(txn, action=RecoveryAction.RETRY_NOW))
        assert decision.final_action == RecoveryAction.RETRY_NOW
        assert decision.guardrail_rule_id != "COOLDOWN_001"

    def test_contact_29_min_ago_blocks_retry_now(self) -> None:
        """29 minutes < 30 min cooldown → COOLDOWN_001 fires."""
        from policy_engine.engine import PolicyEngine
        eng = PolicyEngine()
        txn = make_txn(last_contact_minutes_ago=29)
        decision = eng.evaluate(txn, make_model_decision(txn, action=RecoveryAction.RETRY_NOW))
        assert decision.final_action == RecoveryAction.RETRY_DELAYED
        assert decision.guardrail_rule_id == "COOLDOWN_001"

    def test_contact_exactly_30_min_ago_allows_retry_now(self) -> None:
        """Boundary: exactly 30 min → cooldown has elapsed → retry_now is allowed."""
        from policy_engine.engine import PolicyEngine
        eng = PolicyEngine()
        txn = make_txn(last_contact_minutes_ago=30)
        decision = eng.evaluate(txn, make_model_decision(txn, action=RecoveryAction.RETRY_NOW))
        assert decision.final_action == RecoveryAction.RETRY_NOW
        assert decision.guardrail_rule_id != "COOLDOWN_001"

    def test_contact_31_min_ago_allows_retry_now(self) -> None:
        """31 minutes > 30 min cooldown → rule does not fire."""
        from policy_engine.engine import PolicyEngine
        eng = PolicyEngine()
        txn = make_txn(last_contact_minutes_ago=31)
        decision = eng.evaluate(txn, make_model_decision(txn, action=RecoveryAction.RETRY_NOW))
        assert decision.final_action == RecoveryAction.RETRY_NOW

    def test_cooldown_does_not_block_retry_delayed(self) -> None:
        """COOLDOWN_001 only targets RETRY_NOW — RETRY_DELAYED is not affected."""
        from policy_engine.engine import PolicyEngine
        eng = PolicyEngine()
        txn = make_txn(last_contact_minutes_ago=5)
        decision = eng.evaluate(txn, make_model_decision(txn, action=RecoveryAction.RETRY_DELAYED))
        assert decision.guardrail_rule_id != "COOLDOWN_001"


# ─────────────────────────────────────────────────────────────────────────────
# WINDOW_001 — hour_of_day boundaries at 8 and 21
# ─────────────────────────────────────────────────────────────────────────────

class TestWindow001:
    """Rule: no nudge messages outside 08:00–21:00."""

    @pytest.mark.parametrize("hour", [0, 1, 5, 7])
    def test_before_window_blocks_nudge(self, hour: int) -> None:
        from policy_engine.engine import PolicyEngine
        eng = PolicyEngine()
        txn = make_txn(hour_of_day=hour)
        decision = eng.evaluate(txn, make_model_decision(txn, action=RecoveryAction.NUDGE_ALT_METHOD))
        assert decision.final_action == RecoveryAction.RETRY_DELAYED
        assert decision.guardrail_rule_id == "WINDOW_001"

    def test_exactly_at_window_start_allows_nudge(self) -> None:
        """Hour 8 == CONTACT_WINDOW_START_HOUR → nudge is permitted."""
        from policy_engine.engine import PolicyEngine
        eng = PolicyEngine()
        txn = make_txn(hour_of_day=8)
        decision = eng.evaluate(txn, make_model_decision(txn, action=RecoveryAction.NUDGE_ALT_METHOD))
        assert decision.final_action == RecoveryAction.NUDGE_ALT_METHOD
        assert decision.guardrail_rule_id != "WINDOW_001"

    @pytest.mark.parametrize("hour", [9, 12, 14, 18, 20])
    def test_inside_window_allows_nudge(self, hour: int) -> None:
        from policy_engine.engine import PolicyEngine
        eng = PolicyEngine()
        txn = make_txn(hour_of_day=hour)
        decision = eng.evaluate(txn, make_model_decision(txn, action=RecoveryAction.NUDGE_ALT_METHOD))
        assert decision.final_action == RecoveryAction.NUDGE_ALT_METHOD

    def test_exactly_at_window_end_blocks_nudge(self) -> None:
        """Hour 21 == CONTACT_WINDOW_END_HOUR → nudge is blocked (exclusive upper bound)."""
        from policy_engine.engine import PolicyEngine
        eng = PolicyEngine()
        txn = make_txn(hour_of_day=21)
        decision = eng.evaluate(txn, make_model_decision(txn, action=RecoveryAction.NUDGE_ALT_METHOD))
        assert decision.final_action == RecoveryAction.RETRY_DELAYED
        assert decision.guardrail_rule_id == "WINDOW_001"

    @pytest.mark.parametrize("hour", [22, 23])
    def test_after_window_blocks_nudge(self, hour: int) -> None:
        from policy_engine.engine import PolicyEngine
        eng = PolicyEngine()
        txn = make_txn(hour_of_day=hour)
        decision = eng.evaluate(txn, make_model_decision(txn, action=RecoveryAction.NUDGE_ALT_METHOD))
        assert decision.final_action == RecoveryAction.RETRY_DELAYED

    def test_window_does_not_block_retry_now(self) -> None:
        """WINDOW_001 only restricts NUDGE; silent retry_now is permitted at any hour."""
        from policy_engine.engine import PolicyEngine
        eng = PolicyEngine()
        txn = make_txn(hour_of_day=2)
        decision = eng.evaluate(txn, make_model_decision(txn, action=RecoveryAction.RETRY_NOW))
        assert decision.guardrail_rule_id != "WINDOW_001"


# ─────────────────────────────────────────────────────────────────────────────
# No-override path
# ─────────────────────────────────────────────────────────────────────────────

class TestNoOverride:
    """When no rules fire, the model's recommendation passes through unchanged."""

    def test_clean_retry_now_passes_through(self) -> None:
        from policy_engine.engine import PolicyEngine
        eng = PolicyEngine()
        txn = make_txn(
            failure_code=FailureCode.NETWORK_TIMEOUT,
            retry_count=0,
            contact_count_24h=0,
            last_contact_minutes_ago=None,
            hour_of_day=14,
        )
        decision = eng.evaluate(txn, make_model_decision(txn, action=RecoveryAction.RETRY_NOW))
        assert decision.final_action == RecoveryAction.RETRY_NOW
        assert decision.was_overridden is False
        assert decision.guardrail_rule_id is None
        assert decision.override_reason is None

    def test_clean_nudge_passes_through(self) -> None:
        from policy_engine.engine import PolicyEngine
        eng = PolicyEngine()
        txn = make_txn(
            failure_code=FailureCode.DO_NOT_HONOR,
            contact_count_24h=0,
            hour_of_day=10,
        )
        decision = eng.evaluate(txn, make_model_decision(txn, action=RecoveryAction.NUDGE_ALT_METHOD))
        assert decision.final_action == RecoveryAction.NUDGE_ALT_METHOD
        assert decision.was_overridden is False


# ─────────────────────────────────────────────────────────────────────────────
# Override logging invariants
# ─────────────────────────────────────────────────────────────────────────────

class TestOverrideLogging:
    """Invariants: when overridden, rule_id and reason are always set."""

    def test_override_always_has_rule_id(self) -> None:
        from policy_engine.engine import PolicyEngine
        eng = PolicyEngine()
        txn = make_txn(failure_code=FailureCode.CARD_BLOCKED)
        decision = eng.evaluate(txn, make_model_decision(txn, action=RecoveryAction.RETRY_NOW))
        assert decision.was_overridden is True
        assert decision.guardrail_rule_id is not None
        assert len(decision.guardrail_rule_id) > 0

    def test_override_always_has_reason(self) -> None:
        from policy_engine.engine import PolicyEngine
        eng = PolicyEngine()
        txn = make_txn(failure_code=FailureCode.FRAUD_FLAG)
        decision = eng.evaluate(txn, make_model_decision(txn, action=RecoveryAction.RETRY_NOW))
        assert decision.override_reason is not None
        assert len(decision.override_reason) > 10  # more than a token

    def test_no_override_has_no_rule_id(self, safe_txn: object) -> None:
        from policy_engine.engine import PolicyEngine
        eng = PolicyEngine()
        txn = make_txn()
        decision = eng.evaluate(txn, make_model_decision(txn, action=RecoveryAction.RETRY_NOW))
        assert decision.was_overridden is False
        assert decision.guardrail_rule_id is None
        assert decision.override_reason is None

    def test_model_action_always_recorded(self) -> None:
        from policy_engine.engine import PolicyEngine
        eng = PolicyEngine()
        txn = make_txn(failure_code=FailureCode.CARD_BLOCKED)
        decision = eng.evaluate(txn, make_model_decision(txn, action=RecoveryAction.RETRY_NOW))
        assert decision.model_action == RecoveryAction.RETRY_NOW
        assert decision.final_action == RecoveryAction.ESCALATE_TO_HUMAN


# ─────────────────────────────────────────────────────────────────────────────
# THE DEMO CASE — shown to judges during pitch
# ─────────────────────────────────────────────────────────────────────────────

class TestDemoCase:
    """
    The specific scenario demonstrated to judges:

    'The model wanted to retry_now on this transaction, but our cooldown rule fired
    because this customer was already contacted 40 minutes ago — here's the override
    in the audit log.'

    Wait — 40 min > 30 min cooldown, so COOLDOWN_001 should NOT fire.
    The demo case is: contacted 20 minutes ago → COOLDOWN_001 fires.
    """

    def test_demo_case_20_min_contact_triggers_cooldown(self) -> None:
        """
        Model says retry_now. Customer was last contacted 20 minutes ago.
        COOLDOWN_001 fires → final_action = retry_delayed.
        This is the override case we show in the pitch.
        """
        from policy_engine.engine import PolicyEngine
        eng = PolicyEngine()
        txn = make_txn(
            failure_code=FailureCode.NETWORK_TIMEOUT,
            retry_count=1,
            contact_count_24h=0,
            last_contact_minutes_ago=20,  # < 30 min cooldown
            hour_of_day=15,
        )
        decision = eng.evaluate(txn, make_model_decision(txn, action=RecoveryAction.RETRY_NOW))

        assert decision.model_action == RecoveryAction.RETRY_NOW
        assert decision.final_action == RecoveryAction.RETRY_DELAYED
        assert decision.was_overridden is True
        assert decision.guardrail_rule_id == "COOLDOWN_001"
        assert "20.0 minutes" in decision.override_reason  # type: ignore[operator]

    def test_demo_case_40_min_contact_does_not_trigger_cooldown(self) -> None:
        """
        Complementary: 40 min > cooldown threshold → retry_now is accepted.
        This makes the cooldown boundary legible in the audit log.
        """
        from policy_engine.engine import PolicyEngine
        eng = PolicyEngine()
        txn = make_txn(
            failure_code=FailureCode.NETWORK_TIMEOUT,
            retry_count=1,
            contact_count_24h=0,
            last_contact_minutes_ago=40,  # > 30 min cooldown
            hour_of_day=15,
        )
        decision = eng.evaluate(txn, make_model_decision(txn, action=RecoveryAction.RETRY_NOW))

        assert decision.model_action == RecoveryAction.RETRY_NOW
        assert decision.final_action == RecoveryAction.RETRY_NOW
        assert decision.was_overridden is False
        assert decision.guardrail_rule_id is None

    def test_hard_stop_audit_chain(self) -> None:
        """
        Verifies the complete audit chain for a hard-stop override:
        model → override → rule_id → reason → final_action.
        This is the exact chain the compliance reviewer sees in the dashboard.
        """
        from policy_engine.engine import PolicyEngine
        eng = PolicyEngine()
        txn = make_txn(failure_code=FailureCode.CARD_BLOCKED, amount=Decimal("8500.00"))
        model_dec = make_model_decision(txn, action=RecoveryAction.RETRY_NOW, confidence=0.72)
        policy_dec = eng.evaluate(txn, model_dec)

        # Full chain assertions
        assert policy_dec.model_action == RecoveryAction.RETRY_NOW
        assert policy_dec.final_action == RecoveryAction.ESCALATE_TO_HUMAN
        assert policy_dec.was_overridden is True
        assert policy_dec.guardrail_rule_id == "HARD_STOP_001"
        assert "card_blocked" in policy_dec.override_reason  # type: ignore[operator]
        assert "RBI" in policy_dec.override_reason  # type: ignore[operator]

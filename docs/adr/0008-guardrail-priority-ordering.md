# ADR 0008 - Guardrail Rule Priority Ordering

Date: 2024-08-15
Status: Accepted

## Context

The policy engine evaluates guardrail rules in a fixed priority order (first rule that fires
wins). When two rules could both apply to the same transaction, the ordering determines
which one takes effect. Three rules introduced in this session required explicit priority
reasoning: HARD_STOP_001, OPT_OUT_001, and COST_001.

## Decision

### HARD_STOP_001 is ordered BEFORE OPT_OUT_001

**HARD_STOP_001 fires for:** card_blocked, fraud_flag, kyc_hold, stolen_card  
**Action produced:** ESCALATE_TO_HUMAN  
**OPT_OUT_001 fires for:** customer_opted_out=True  
**Action produced:** STOP

The question: if a customer has revoked DPDP consent AND the transaction has a
fraud/KYC/stolen-card failure code, which rule wins?

**HARD_STOP_001 must win. Reasoning:**

1. **Nature of the action.** ESCALATE_TO_HUMAN is an *internal compliance routing
   action* — it routes the case to a human fraud analyst. It does not send any
   automated message to the customer. There is no "automated contact" with the
   data principal.

2. **Scope of DPDP consent revocation.** DPDP Act 2023, Chapter III ("Rights of
   Data Principal") grants the right to withdraw consent from *automated data
   processing for commercial purposes* — i.e., automated retries, nudge messages,
   and recovery communications directed at the customer. It does not govern internal
   fraud case routing obligations of the payment processor.

3. **RBI statutory obligation supersedes contractual/consent constraints.** Fraud
   escalation duties under RBI Master Direction on Fraud (RBI/2016-17/274) and the
   Payment and Settlement Systems Act, 2007 are non-waivable statutory obligations.
   A customer cannot revoke consent to have a stolen card report or fraud flag
   escalated to human review — this obligation exists independently of any
   consent framework.

4. **Practical consequence of the alternative.** If OPT_OUT_001 fired first, a
   fraudulent transaction on a stolen card with customer_opted_out=True would
   produce STOP (no further action) instead of ESCALATE_TO_HUMAN. This would mean
   the system silently drops a mandatory fraud-review case — a compliance violation
   far more serious than a consent misstep.

**Therefore:** HARD_STOP_001 ? position 1, OPT_OUT_001 ? position 2.

---

### OPT_OUT_001 is ordered BEFORE COST_001

DPDP consent revocation is an absolute right of the data principal. If the customer
has explicitly revoked consent, the economic argument (whether further retries are
cost-justified) is moot — we must not contact them regardless of cost ratios.

**Therefore:** OPT_OUT_001 ? position 2, COST_001 ? position 3.

---

### OPT_OUT_001 is ordered BEFORE HARD_STOP_002

HARD_STOP_002 produces NUDGE_ALT_METHOD (sends a message to the customer to use a
different payment instrument). This is exactly the category of automated recovery
contact that DPDP consent revocation governs. If the customer has revoked consent,
this nudge must not be sent.

However, HARD_STOP_001 has already been evaluated at position 1, so the only cases
reaching OPT_OUT_001 have already passed through the fraud/KYC filter. HARD_STOP_002
at position 4 is correctly below OPT_OUT_001 because nudging a customer who has
revoked consent would be a DPDP violation.

---

## Final Priority Order

| Priority | Rule ID | Rationale |
|---|---|---|
| 1 | HARD_STOP_001 | RBI statutory fraud/KYC escalation — supersedes consent |
| 2 | OPT_OUT_001 | DPDP consent revocation — stops all automated customer contact |
| 3 | COST_001 | Economic stop — only applies when consent is valid |
| 4 | HARD_STOP_002 | Instrument-level hard stop — NUDGE is customer contact, blocked by consent |
| 5 | RATE_LIMIT_001 | Retry cap |
| 6 | RATE_LIMIT_002 | Contact-per-24h cap (DPDP) |
| 7 | COOLDOWN_001 | Gateway deduplication cooldown |
| 8 | WINDOW_001 | TRAI DND contact window |

## Consequences

- A customer who has opted out AND has a fraud-flagged transaction will still be
  escalated to human review (HARD_STOP_001 wins). This is the correct and legally
  required behaviour.
- A customer who has opted out with a soft-decline code (e.g. insufficient_funds)
  will receive STOP — no retry, no nudge, no contact.
- The ordering is explicitly documented in engine.py RULE_PRIORITY comments.
- Tests in test_new_rules_and_schema.py::TestOptOut001 explicitly verify both cases.

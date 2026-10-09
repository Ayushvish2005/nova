"""Pure action policy; no network transport, payments, settlement or acceptance."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from .audit_models import Assessment, CaseScope


@dataclass
class SafetyProof:
    evidence_complete: bool = False
    terms_in_force: bool = False
    pickup_supported: bool = False
    authority_supported: bool = False
    recipient: str | None = None


@dataclass
class PreparedFinding:
    assessment: Assessment
    amount: Decimal | None = None
    amount_range: list[Decimal] | None = None
    proof: SafetyProof = field(default_factory=SafetyProof)
    learned_at: dict | None = None


def scopes_overlap(a: CaseScope, b: CaseScope) -> bool:
    for field_name in ("contract_id", "lane", "charge"):
        first, second = getattr(a, field_name), getattr(b, field_name)
        if first is not None and second is not None and first != second:
            return False
    if a.subject_date and a.period_end and b.subject_date and b.period_end:
        if a.period_end < b.subject_date or b.period_end < a.subject_date:
            return False
    elif a.period is not None and b.period is not None and a.period != b.period:
        return False
    return True


def conflict_blocks(candidate: Assessment, conflict: Assessment) -> bool:
    if candidate.key.identity() == conflict.key.identity():
        return True
    if candidate.key.contract_id and conflict.key.contract_id and candidate.key.contract_id != conflict.key.contract_id:
        return False
    if not candidate.scopes or not conflict.scopes:
        # Unknown scope cannot establish that a held conflict is unrelated.
        return candidate.key.contract_id == conflict.key.contract_id
    return any(scopes_overlap(a, b) for a in candidate.scopes for b in conflict.scopes)


def select_actions(items: list[PreparedFinding], mode: str, as_of: date) -> dict[tuple, str]:
    if mode not in ("supervised", "autonomous"):
        raise ValueError("unknown reporting mode")
    totals: dict[tuple, Decimal] = {}
    conflicts = [i.assessment for i in items if i.assessment.verdict == "conflict"]

    def group(item: Assessment) -> tuple:
        return item.key.identity() if item.key.finding_class == "cashback" else ("invoice", item.key.invoice_id)

    for item in items:
        a = item.assessment
        if a.verdict == "finding" and a.key.finding_class not in ("expiry", "late_fee_risk"):
            totals[group(a)] = totals.get(group(a), Decimal(0)) + (item.amount or Decimal(0))
    actions = {}
    for item in items:
        a, proof = item.assessment, item.proof
        if a.verdict == "no_finding":
            action = "none"
        elif a.verdict == "conflict":
            action = "escalate"
        elif a.verdict == "finding" and a.key.finding_class == "expiry":
            action = "internal:alert"
        elif a.verdict == "finding" and a.key.finding_class == "late_fee_risk":
            action = "internal:payment_reminder"
        elif a.verdict == "finding" and a.key.finding_class == "cashback" and a.credit_due_date and as_of < a.credit_due_date:
            action = "internal:track_credit"
        else:
            remedy = "request_document" if a.verdict == "cannot_determine" else "claim_credit" if a.key.finding_class == "cashback" else "dispute"
            under_threshold = totals.get(group(a), Decimal(0)) < Decimal("5000.00")
            due_known = a.key.finding_class != "cashback" or a.credit_due_date is not None
            if not under_threshold or not due_known:
                action = "human_review"
            elif mode == "supervised":
                action = "draft:" + remedy
            else:
                safe = (proof.evidence_complete and proof.terms_in_force and proof.pickup_supported
                        and proof.authority_supported and proof.recipient is not None
                        and not any(conflict_blocks(a, other) for other in conflicts))
                action = "execute:" + remedy if safe else "human_review"
        actions[a.key.identity()] = action
    return actions

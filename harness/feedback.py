"""Immutable, time-aware human feedback with exact-key and conjunctive scope matching."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

from pydantic import AwareDatetime, model_validator

from .audit_models import Assessment, FindingKey
from .claude_extractor import canonical_json
from .facts import FactModel, Text, invoice_key
from .store import StateStore


class FeedbackFact(FactModel):
    type: Literal["sender_authority", "rate_accepted", "waiver_scope", "precedence", "dispute_closed"]
    subject: dict[str, Any]
    value: dict[str, Any]
    evidence: Text


class FeedbackScope(FactModel):
    kind: Literal["standing", "one_off"]
    applies_to: dict[str, Any]
    valid_from: date
    valid_to: date | None = None

    @model_validator(mode="after")
    def constrained_scope(self) -> FeedbackScope:
        if not self.applies_to:
            raise ValueError("feedback scope cannot be empty")
        if self.kind == "standing" and not self.applies_to.get("contract_id"):
            raise ValueError("standing feedback must name a contract")
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("feedback scope dates are reversed")
        return self


class FeedbackItem(FactModel):
    item_id: Text
    finding_key: FindingKey
    solicited: bool = False
    decision: Literal["confirm", "reject", "close", "override_amount", "resolve_conflict", "one_off_approval"]
    fact: FeedbackFact
    scope: FeedbackScope
    note: str | None = None

    @model_validator(mode="after")
    def decision_value(self) -> FeedbackItem:
        if self.decision == "override_amount":
            try:
                amount = Decimal(str(self.fact.value["amount_usd"]))
            except (KeyError, InvalidOperation) as exc:
                raise ValueError("override_amount needs amount_usd") from exc
            if not amount.is_finite() or amount < 0 or amount > Decimal("1e20"):
                raise ValueError("override amount must be finite and nonnegative")
        if self.decision == "one_off_approval" and self.scope.kind != "one_off":
            raise ValueError("one-off approval cannot grant standing authority")
        if self.fact.type == "sender_authority" and not self.fact.value.get("categories"):
            raise ValueError("sender authority must name its categories")
        return self


class FeedbackEnvelope(FactModel):
    schema_version: Literal["1.0"] = "1.0"
    world: Text
    after_batch: Text
    decided_at: AwareDatetime
    decided_by: dict[str, Any]
    items: list[FeedbackItem]


class StoredFeedback(FactModel):
    world: str
    after_batch: str
    decided_at: AwareDatetime
    item: FeedbackItem


def ingest_feedback(path: str | Path, state_dir: str | Path) -> int:
    envelope = FeedbackEnvelope.model_validate_json(Path(path).read_text())
    decided_at = envelope.decided_at.astimezone(timezone.utc).isoformat()
    with StateStore(state_dir) as store, store.transaction():
        worlds = {row[0] for row in store.connection.execute("SELECT DISTINCT world FROM feedback_items")}
        if worlds and worlds != {envelope.world}:
            raise ValueError("feedback from different worlds must use separate state directories")
        added = 0
        for item in envelope.items:
            record = (envelope.after_batch, decided_at, canonical_json(envelope.decided_by),
                      canonical_json(item.model_dump(mode="json", by_alias=True)))
            old = store.connection.execute(
                "SELECT after_batch, decided_at, actor_json, payload_json FROM feedback_items WHERE world = ? AND item_id = ?",
                (envelope.world, item.item_id),
            ).fetchone()
            if old is not None:
                if tuple(old) != record:
                    raise ValueError(f"feedback item {item.item_id} changed; use a new item_id for revisions")
                continue
            store.connection.execute("INSERT INTO feedback_items VALUES (?, ?, ?, ?, ?, ?)", (envelope.world, item.item_id, *record))
            added += 1
    return added


def load_feedback(store: StateStore, as_of: date) -> list[StoredFeedback]:
    result = []
    for row in store.connection.execute("SELECT * FROM feedback_items ORDER BY decided_at, item_id"):
        if datetime.fromisoformat(row["decided_at"]).astimezone(timezone.utc).date() > as_of:
            continue
        result.append(StoredFeedback.model_validate_json(canonical_json({
            "world": row["world"], "after_batch": row["after_batch"], "decided_at": row["decided_at"],
            "item": json.loads(row["payload_json"]),
        })))
    return result


def matches(feedback: StoredFeedback, assessment: Assessment, as_of: date) -> bool:
    item = feedback.item
    if feedback.decided_at.astimezone(timezone.utc).date() > as_of:
        return False
    same_key = item.finding_key.identity() == assessment.key.identity()
    if item.scope.kind == "one_off" and not same_key:
        return False
    if item.decision == "one_off_approval" and item.fact.type == "waiver_scope" and item.fact.value.get("months"):
        periods = {scope.subject_date.strftime("%Y-%m") for scope in assessment.scopes if scope.subject_date}
        periods.update(scope.period for scope in assessment.scopes if scope.period and len(scope.period) == 7 and scope.period[4] == "-")
        if not periods or not periods <= set(item.fact.value["months"]):
            return False
    # Decisions on a named key are effective after the decision; standing claims
    # additionally need the covered shipment/document date, not its receipt date.
    direct_decision = same_key and item.decision in ("reject", "close", "override_amount", "one_off_approval")
    if direct_decision:
        return True
    contexts = assessment.scopes or [None]
    for scope in contexts:
        context = assessment.key.model_dump(by_alias=True)
        if scope is not None:
            context.update({k: v for k, v in scope.model_dump().items() if v is not None})
        match = True
        for key, expected in item.scope.applies_to.items():
            actual = context.get(key)
            if key == "invoice_id" and isinstance(expected, str):
                expected = invoice_key(expected)
            if actual is None or (actual not in expected if isinstance(expected, list) else actual != expected):
                match = False
                break
        if not match:
            continue
        subject_date = scope.subject_date if scope else None
        if subject_date is None:
            continue
        if not direct_decision and (subject_date < item.scope.valid_from or (item.scope.valid_to and subject_date > item.scope.valid_to)):
            continue
        if item.fact.type == "sender_authority" and context.get("category") not in item.fact.value.get("categories", []):
            continue
        return True
    return False

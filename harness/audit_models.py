"""Typed audit proposals: Claude interprets clauses; host code computes and gates actions."""
from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .facts import Evidence, FactModel, Text, invoice_key

FindingClass = Literal["overcharge", "duplicate", "waived_but_billed", "late_fee_not_owed", "cashback", "expiry", "late_fee_risk"]


class FindingKey(FactModel):
    finding_class: FindingClass = Field(alias="class")
    invoice_id: str | None = None
    contract_id: str | None = None
    period: str | None = None
    credit_type: Literal["rebate", "margin_step_down", "promised_credit"] | None = None

    @model_validator(mode="after")
    def key_shape(self) -> FindingKey:
        if self.finding_class == "cashback":
            if not self.contract_id or not self.period or not self.credit_type:
                raise ValueError("cashback requires contract, period and credit type")
            import re
            if not re.fullmatch(r"\d{4}-Q[1-4]|CY[1-9]\d*", self.period):
                raise ValueError("invalid cashback period")
        elif self.finding_class == "expiry":
            if not self.contract_id:
                raise ValueError("expiry requires contract_id")
        elif not self.invoice_id:
            raise ValueError("invoice finding requires invoice_id")
        if self.invoice_id:
            self.invoice_id = invoice_key(self.invoice_id)
        return self

    def identity(self) -> tuple[str, ...]:
        if self.finding_class == "cashback":
            return self.finding_class, self.contract_id, self.period, self.credit_type
        return self.finding_class, self.contract_id if self.finding_class == "expiry" else self.invoice_id


class Expression(FactModel):
    op: Literal["fact", "constant", "add", "subtract", "multiply", "divide", "round_money", "ceil", "min", "max", "days_between"]
    fact_id: str | None = None
    path: str | None = None
    constant: str | None = None
    args: list[Expression] = Field(default_factory=list)


class CaseScope(FactModel):
    """Each relevant lane/charge/date/person; never use a broad string wildcard."""
    contract_id: str | None = None
    lane: str | None = None
    charge: str | None = None
    person: str | None = None
    category: str | None = None
    party: str | None = None
    shipment_id: str | None = None
    period: str | None = None
    subject_date: date | None = None
    period_end: date | None = None


class MissingHolder(FactModel):
    party: str | None = None
    person: str | None = None
    role: str | None = None

    @model_validator(mode="after")
    def named_holder(self) -> MissingHolder:
        if not self.party and not self.person:
            raise ValueError("missing-document holder needs a party or person")
        return self


class MissingDocument(FactModel):
    name: Text
    holder: MissingHolder
    category: Text = Field(description="Schedule 1 category for this document: rates, treasury, payment_terms, notices, etc.")


class Assessment(FactModel):
    key: FindingKey
    verdict: Literal["finding", "no_finding", "conflict", "cannot_determine"]
    rationale: Text
    evidence: Annotated[list[Evidence], Field(min_length=1)]
    confidence: Annotated[float, Field(ge=0, le=1)]
    scopes: list[CaseScope] = Field(default_factory=list)
    amount: Expression | None = None
    conflict_amounts: list[Expression] = Field(default_factory=list)
    deadline: date | None = None
    credit_due_date: date | None = None
    period_end: date | None = None
    missing_doc: MissingDocument | None = None
    term_fact_ids: list[str] = Field(default_factory=list)
    operational_fact_ids: list[str] = Field(default_factory=list)
    feedback_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent_verdict(self) -> Assessment:
        if self.verdict == "finding" and self.key.finding_class != "expiry" and self.amount is None:
            raise ValueError("monetary finding needs a source-backed calculation")
        if self.verdict == "conflict" and (len(self.conflict_amounts) != 2 or len(self.evidence) < 2):
            raise ValueError("conflict needs two calculations and at least two evidence entries")
        if self.verdict == "cannot_determine" and self.missing_doc is None:
            raise ValueError("cannot_determine needs the missing document and holder")
        if self.key.finding_class in ("expiry", "late_fee_risk") and self.deadline is None:
            raise ValueError("dated finding requires deadline")
        if self.key.finding_class == "cashback" and self.period_end is None:
            raise ValueError("cashback requires period_end")
        return self


class AssessmentBundle(FactModel):
    assessments: list[Assessment]

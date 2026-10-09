"""Normalized source claims, not audit conclusions or selected/operative terms."""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Text = Annotated[str, Field(min_length=1)]
Number = Annotated[Decimal, Field(allow_inf_nan=False)]


class FactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_default=True)


class Evidence(FactModel):
    doc: Text
    page: Annotated[int, Field(ge=1)]
    quote: Text
    layer: Literal["native", "ocr", "html"] = "native"

    @field_validator("quote")
    @classmethod
    def substantial_quote(cls, value: str) -> str:
        if len("".join(value.split())) < 12:
            raise ValueError("evidence needs at least 12 non-whitespace characters")
        return value


class ObservedDate(FactModel):
    raw: Text
    value: date | None = Field(default=None, description="ISO date only when unambiguous; never infer a missing year")


class Party(FactModel):
    name: Text
    identifier: str | None = None
    role: str | None = None


class EntityReference(FactModel):
    entity_type: Literal["invoice", "contract", "amendment", "shipment", "document", "other"]
    identifier: Text
    relation: Text
    doc_ref: str | None = Field(default=None, description="Exact known path, or null for unresolved references")


class Scope(FactModel):
    kind: Literal["one_off", "period", "standing", "unknown"] = "unknown"
    invoice_ids: list[str] = Field(default_factory=list)
    shipment_ids: list[str] = Field(default_factory=list)
    lanes: list[str] = Field(default_factory=list)
    charge_labels: list[str] = Field(default_factory=list)
    period: str | None = None
    starts_on: ObservedDate | None = None
    ends_on: ObservedDate | None = None
    text: str | None = None


class StatedValue(FactModel):
    """Open vocabulary for new clauses, bands, thresholds, units and charge aliases."""

    name: Text
    raw: Text
    number: Number | None = None
    date_value: ObservedDate | None = None
    unit: str | None = None
    currency: Annotated[str, Field(pattern="^[A-Z]{3}$")] | None = None


class FactBase(FactModel):
    evidence: Annotated[list[Evidence], Field(min_length=1)]
    confidence: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
    assertion_mode: Literal["direct", "quoted", "forwarded", "unknown"] = "unknown"
    asserted_by: str | None = None
    references: list[EntityReference] = Field(default_factory=list)


def invoice_key(value: str) -> str:
    """Identifier normalization defined by the glossary; preserve the printed value too."""
    return re.sub(r"[\s#:]", "", value.upper())


class Invoice(FactBase):
    kind: Literal["invoice"] = "invoice"
    invoice_number: Text
    document_type: Literal["invoice", "credit_note", "statement"]
    contract_id: str | None = None
    issuer: Party | None = None
    customer: Party | None = None
    invoice_date: ObservedDate | None = None
    due_date: ObservedDate | None = None
    currency: Annotated[str, Field(pattern="^[A-Z]{3}$")] | None = None
    subtotal: Number | None = None
    tax: Number | None = None
    total: Number | None = None
    period: str | None = None
    original_invoice_ids: list[str] = Field(default_factory=list)

    @property
    def invoice_id(self) -> str:
        return invoice_key(self.invoice_number)


class InvoiceLine(FactBase):
    kind: Literal["invoice_line"] = "invoice_line"
    invoice_number: Text
    line_number: str | None = None
    description: Text
    charge_code: str | None = Field(default=None, description="Only an explicitly printed code; do not map labels by memory")
    shipment_id: str | None = None
    lane: str | None = None
    service_date: ObservedDate | None = None
    quantity: Number | None = None
    unit: str | None = None
    rate: Number | None = None
    rate_unit: str | None = None
    basis: str | None = None
    amount: Number | None = None
    currency: Annotated[str, Field(pattern="^[A-Z]{3}$")] | None = None

    @property
    def invoice_id(self) -> str:
        return invoice_key(self.invoice_number)


class Contract(FactBase):
    kind: Literal["contract"] = "contract"
    contract_id: Text
    parties: list[Party] = Field(default_factory=list)
    agreement_date: ObservedDate | None = None
    effective_date: ObservedDate | None = None
    expiry_date: ObservedDate | None = None
    signature_status: Literal["signed", "unsigned", "draft", "unknown"] = "unknown"
    signatories: list[str] = Field(default_factory=list)
    renewal_text: str | None = None


class Amendment(FactBase):
    kind: Literal["amendment"] = "amendment"
    amendment_id: Text
    contract_id: Text
    effective_date: ObservedDate | None = None
    signed_date: ObservedDate | None = None
    signature_status: Literal["signed", "unsigned", "draft", "unknown"] = "unknown"
    scope: Scope = Field(default_factory=Scope)
    changes: Text
    supersedes_ids: list[str] = Field(default_factory=list)


class ContractTerm(FactBase):
    kind: Literal["term"] = "term"
    contract_id: str | None = None
    instrument_id: str | None = None
    category: Text = Field(description="E.g. rate, fuel, accessorial, charge_alias, payment, rebate, fx, precedence; open vocabulary")
    clause: str | None = None
    text: Text
    scope: Scope = Field(default_factory=Scope)
    values: list[StatedValue] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)


class Authority(FactBase):
    """A source's stated delegation, not a determination of anyone's authority."""

    kind: Literal["authority"] = "authority"
    contract_id: str | None = None
    name: Text
    party: str | None = None
    title: str | None = None
    email: str | None = None
    categories: Annotated[list[str], Field(min_length=1)]


class Waiver(FactBase):
    kind: Literal["waiver"] = "waiver"
    contract_id: str | None = None
    text: Text
    scope: Scope
    conditions: list[str] = Field(default_factory=list)
    stated_date: ObservedDate | None = None


class Shipment(FactBase):
    kind: Literal["shipment"] = "shipment"
    shipment_id: Text
    record_type: Literal["awb", "tracking_event", "milestone", "declaration", "customs", "other"]
    master_shipment_id: str | None = None
    lane: str | None = None
    event: str | None = None
    event_date: ObservedDate | None = None
    station: str | None = None
    reason: str | None = None
    pieces: Annotated[int, Field(ge=0)] | None = None
    chargeable_weight: Number | None = None
    actual_weight: Number | None = None
    weight_unit: str | None = None
    details: list[StatedValue] = Field(default_factory=list)


class EmailInformation(FactBase):
    kind: Literal["email"] = "email"
    intent: Text
    statement: Text
    values: list[StatedValue] = Field(default_factory=list)


class Payment(FactBase):
    kind: Literal["payment"] = "payment"
    invoice_number: Text
    status: Text
    paid_date: ObservedDate | None = None
    due_date: ObservedDate | None = None
    amount: Number | None = None
    currency: Annotated[str, Field(pattern="^[A-Z]{3}$")] | None = None
    details: list[StatedValue] = Field(default_factory=list)

    @property
    def invoice_id(self) -> str:
        return invoice_key(self.invoice_number)


Fact = Annotated[
    Invoice | InvoiceLine | Contract | Amendment | ContractTerm | Authority | Waiver | Shipment | EmailInformation | Payment,
    Field(discriminator="kind"),
]


class ExtractionIssue(FactModel):
    code: Text
    description: Text
    evidence: list[Evidence] = Field(default_factory=list)


class FactBundle(FactModel):
    schema_version: Literal[1] = 1
    facts: list[Fact]
    issues: list[ExtractionIssue] = Field(default_factory=list)

    @model_validator(mode="after")
    def explain_empty_result(self) -> FactBundle:
        if not self.facts and not self.issues:
            raise ValueError("an empty extraction must explain why in issues")
        return self

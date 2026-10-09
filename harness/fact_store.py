"""Versioned fact sets, atomically published per source document."""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Iterator

from .claude_extractor import ExtractionInput, StructuredResult, canonical_json, digest
from .fact_validation import validate_evidence
from .facts import (
    Amendment, EntityReference, Fact, FactBundle, Invoice, InvoiceLine, Payment, Shipment, Waiver, invoice_key,
)
from .store import StateStore


def entity_key(fact: Fact) -> tuple[str | None, str | None]:
    if isinstance(fact, (Invoice, InvoiceLine, Payment)):
        return "invoice", fact.invoice_id
    if isinstance(fact, Amendment):
        return "amendment", fact.amendment_id
    if isinstance(fact, Shipment):
        return "shipment", fact.shipment_id
    contract_id = getattr(fact, "contract_id", None)
    return ("contract", contract_id) if contract_id else (None, None)


def relationships(fact: Fact) -> list[EntityReference]:
    """Index explicit IDs, without resolving their authority, precedence or applicability."""
    refs = list(fact.references)

    def add(kind: str, identifier: str | None, relation: str) -> None:
        if identifier:
            refs.append(EntityReference(entity_type=kind, identifier=identifier, relation=relation))

    add("contract", getattr(fact, "contract_id", None), "amends" if isinstance(fact, Amendment) else "references_contract")
    if isinstance(fact, (InvoiceLine, Payment)):
        add("invoice", fact.invoice_number, "line_of" if isinstance(fact, InvoiceLine) else "payment_record_for")
    if isinstance(fact, Invoice):
        for original in fact.original_invoice_ids:
            add("invoice", original, "references_original_invoice")
    if isinstance(fact, InvoiceLine):
        add("shipment", fact.shipment_id, "billed_shipment")
    if isinstance(fact, Shipment):
        add("shipment", fact.master_shipment_id, "house_of")
    if isinstance(fact, Waiver):
        for identifier in fact.scope.invoice_ids:
            add("invoice", identifier, "waiver_names")
        for identifier in fact.scope.shipment_ids:
            add("shipment", identifier, "waiver_names")
    if isinstance(fact, Amendment):
        for identifier in fact.supersedes_ids:
            add("amendment", identifier, "states_supersedes")
    return refs


class FactStore(StateStore):
    @staticmethod
    def extraction_id(item: ExtractionInput, extractor_version: str) -> str:
        return digest([item.document.source.doc_ref, item.document.reader_version, extractor_version, item.input_hash])

    def cached(self, item: ExtractionInput, extractor_version: str) -> bool:
        row = self.connection.execute(
            "SELECT status FROM fact_extractions WHERE extraction_id = ?", (self.extraction_id(item, extractor_version),),
        ).fetchone()
        return row is not None and row["status"] == "ready"

    def activate(self, doc_ref: str, extraction_id: str) -> None:
        if not self.connection.in_transaction:
            raise RuntimeError("fact writes require a transaction")
        self.connection.execute(
            """INSERT INTO active_fact_extractions(doc_ref, extraction_id) VALUES (?, ?)
            ON CONFLICT(doc_ref) DO UPDATE SET extraction_id = excluded.extraction_id
            WHERE active_fact_extractions.extraction_id != excluded.extraction_id""", (doc_ref, extraction_id),
        )

    def save_result(
        self, item: ExtractionInput, extractor_version: str, model: str, *,
        result: StructuredResult | None = None, error: str | None = None, responses: list | None = None,
    ) -> None:
        if not self.connection.in_transaction:
            raise RuntimeError("fact writes require a transaction")
        if (result is None) == (error is None):
            raise ValueError("supply exactly one of result or error")
        current = self.get_document(item.document.source.doc_ref)
        if current is None or current != item.document:
            raise ValueError("document changed during structured extraction")
        if result is not None:
            # Revalidate even results supplied by another backend or model_construct().
            bundle = FactBundle.model_validate_json(result.bundle.model_dump_json())
            validate_evidence(bundle, current, item.known_refs)
        extraction_id = self.extraction_id(item, extractor_version)
        doc_ref = current.source.doc_ref
        self.connection.execute(
            """INSERT INTO fact_extractions(extraction_id, doc_ref, reader_version, extractor_version,
               input_hash, requested_model, status, error, issues_json, responses_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(extraction_id) DO UPDATE SET status = excluded.status, error = excluded.error,
               issues_json = excluded.issues_json, responses_json = excluded.responses_json,
               attempts = fact_extractions.attempts + 1""",
            (extraction_id, doc_ref, current.reader_version, extractor_version, item.input_hash, model,
             "ready" if result else "failed", error,
             canonical_json([issue.model_dump(mode="json") for issue in bundle.issues]) if result else "[]",
             canonical_json(result.responses if result else responses or [])),
        )
        self.connection.execute("DELETE FROM facts WHERE extraction_id = ?", (extraction_id,))
        if result is not None:
            for fact in bundle.facts:
                payload = fact.model_dump(mode="json")
                fact_id = digest([extraction_id, payload])
                entity_type, entity_id = entity_key(fact)
                self.connection.execute(
                    """INSERT OR IGNORE INTO facts(fact_id, extraction_id, doc_ref, kind, entity_type, entity_id, payload_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (fact_id, extraction_id, doc_ref, fact.kind, entity_type, entity_id, canonical_json(payload)),
                )
                for ref in relationships(fact):
                    target_id = invoice_key(ref.identifier) if ref.entity_type == "invoice" else ref.identifier
                    values = [fact_id, ref.relation, ref.entity_type, target_id, ref.doc_ref]
                    self.connection.execute(
                        """INSERT OR IGNORE INTO fact_relationships(relationship_id, fact_id, relation, target_type,
                           target_id, target_doc_ref) VALUES (?, ?, ?, ?, ?, ?)""", (digest(values), *values),
                    )
        # A failed new version is visible as failed, never silently replaced by stale facts.
        self.activate(doc_ref, extraction_id)

    def fact_records(self, *, doc_ref: str | None = None, kind: str | None = None, as_of: str | None = None) -> Iterator[dict]:
        query = """SELECT f.fact_id, f.doc_ref, f.payload_json FROM facts f
            JOIN active_fact_extractions a ON a.doc_ref = f.doc_ref AND a.extraction_id = f.extraction_id
            JOIN fact_extractions e ON e.extraction_id = f.extraction_id
            JOIN documents d ON d.doc_ref = f.doc_ref
            WHERE e.status = 'ready' AND e.reader_version = d.reader_version"""
        params = []
        for column, value in (("f.doc_ref", doc_ref), ("f.kind", kind)):
            if value is not None:
                query += f" AND {column} = ?"
                params.append(value)
        if as_of is not None:
            cutoff = date.fromisoformat(as_of) + timedelta(days=1)
            query += " AND d.received_at < ?"
            params.append(f"{cutoff.isoformat()}T00:00:00Z")
        query += " ORDER BY f.doc_ref, f.fact_id"
        for row in self.connection.execute(query, params):
            yield {"fact_id": row["fact_id"], "doc_ref": row["doc_ref"], "fact": json.loads(row["payload_json"])}

    def facts(self, *, doc_ref: str | None = None, kind: str | None = None, as_of: str | None = None) -> Iterator[Fact]:
        for record in self.fact_records(doc_ref=doc_ref, kind=kind, as_of=as_of):
            yield FactBundle.model_validate_json(canonical_json({"facts": [record["fact"]]})).facts[0]

    def status(self, doc_ref: str) -> dict | None:
        row = self.connection.execute(
            """SELECT e.* FROM fact_extractions e JOIN active_fact_extractions a
               ON a.extraction_id = e.extraction_id WHERE a.doc_ref = ?""", (doc_ref,),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["issues"] = json.loads(result.pop("issues_json"))
        result["responses"] = json.loads(result.pop("responses_json"))
        return result

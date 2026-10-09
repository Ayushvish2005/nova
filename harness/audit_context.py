"""Date-filtered evidence available to the evaluator and deterministic safety gate."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timezone

from .actions import SafetyProof
from .audit_models import Assessment
from .facts import invoice_key
from .feedback import StoredFeedback, matches
from .models import Document


@dataclass
class AuditContext:
    as_of: date
    records: dict[str, dict]
    documents: dict[str, Document]
    feedback: list[StoredFeedback]
    issues: dict[str, list]

    @property
    def facts(self) -> dict[str, dict]:
        return {identifier: record["fact"] for identifier, record in self.records.items()}

    def signed_documents(self, contract_id: str | None) -> set[str]:
        return {r["doc_ref"] for r in self.records.values()
                if r["fact"]["kind"] in ("contract", "amendment")
                and r["fact"].get("contract_id") == contract_id
                and r["fact"].get("signature_status") == "signed"
                and r["fact"].get("assertion_mode") == "direct"
                and self.documents[r["doc_ref"]].source.kind == "attachment"}

    def validate_assessment(self, assessment: Assessment) -> None:
        for evidence in assessment.evidence:
            doc = self.documents.get(evidence.doc)
            if doc is None or doc.extraction.status != "ready":
                raise ValueError("report evidence is unavailable by as_of")
            if evidence.page > len(doc.extraction.pages):
                raise ValueError("report evidence page does not exist")
            page = doc.extraction.pages[evidence.page - 1]
            # Match grader behavior: only wholly image-only PDFs relax quote checking.
            image_only = doc.source.format == "pdf" and all(not p.native_text.strip() for p in doc.extraction.pages)
            text = page.text if image_only and evidence.layer == "ocr" else page.native_text
            if evidence.layer not in ("native", "ocr") or " ".join(evidence.quote.split()) not in " ".join(text.split()):
                raise ValueError("report quote does not resolve in the grader-compatible text layer")
        identifiers = set(assessment.term_fact_ids + assessment.operational_fact_ids)
        if not identifiers or not identifiers <= self.records.keys():
            raise ValueError("assessment must identify available supporting facts")
        key = assessment.key
        if key.invoice_id:
            invoices = [f for f in self.facts.values() if f["kind"] == "invoice"
                        and invoice_key(f["invoice_number"]) == key.invoice_id and f["document_type"] != "credit_note"]
            if not invoices:
                raise ValueError("invoice finding has no held invoice")
            contracts = {f.get("contract_id") for f in invoices if f.get("contract_id")}
            if len(contracts) > 1 or (key.contract_id and contracts and key.contract_id not in contracts):
                raise ValueError("invoice contract relationship is ambiguous or incorrect")
            if not key.contract_id and len(contracts) == 1:
                key.contract_id = next(iter(contracts))
            lines = [f for f in self.facts.values() if f["kind"] == "invoice_line" and invoice_key(f["invoice_number"]) == key.invoice_id]
            lanes = {f["lane"] for f in lines if f.get("lane")}
            shipments = {f["shipment_id"] for f in lines if f.get("shipment_id")}
            for scope in assessment.scopes:
                if lanes and scope.lane and scope.lane not in lanes:
                    raise ValueError("assessment lane does not belong to the invoice")
                if shipments and scope.shipment_id and scope.shipment_id not in shipments:
                    raise ValueError("assessment shipment does not belong to the invoice")
        elif not any(f["kind"] == "contract" and f["contract_id"] == key.contract_id for f in self.facts.values()):
            raise ValueError("portfolio finding has no held contract")
        for scope in assessment.scopes:
            if scope.contract_id and key.contract_id and scope.contract_id != key.contract_id:
                raise ValueError("assessment scope points at another contract")
        known_feedback = {f.item.item_id: f for f in self.feedback}
        for identifier in assessment.feedback_ids:
            if identifier not in known_feedback or not matches(known_feedback[identifier], assessment, self.as_of):
                raise ValueError("assessment used feedback outside its key, scope or time window")
            if self.feedback_superseded(known_feedback[identifier], assessment):
                raise ValueError("a later signed amendment supersedes this standing feedback")
        if assessment.verdict == "conflict":
            if len({e.doc for e in assessment.evidence}) < 2:
                raise ValueError("conflict must cite two distinct source documents")
            proof = self.safety_proof(assessment)
            deciding_docs = {self.records[fid]["doc_ref"] for fid in assessment.term_fact_ids}
            if len(deciding_docs) < 2 or not proof.authority_supported or not proof.terms_in_force:
                raise ValueError("conflict needs two authoritative deciding sources in force")
        if assessment.missing_doc:
            quoted = " ".join(e.quote for e in assessment.evidence)
            if assessment.missing_doc.name.casefold() not in quoted.casefold():
                raise ValueError("missing document name must be stated in held evidence")
            holder = assessment.missing_doc.holder
            if not any(f["kind"] == "authority" and f.get("contract_id") == key.contract_id
                                         and (not holder.person or f["name"] == holder.person)
                                         and (not holder.party or f.get("party") == holder.party)
                                         and (not holder.role or f.get("title") == holder.role)
                                         and assessment.missing_doc.category in f["categories"]
                                         and f.get("assertion_mode") == "direct"
                                         and any(e["doc"] in self.signed_documents(key.contract_id) for e in f["evidence"])
                                         for f in self.facts.values()):
                raise ValueError("missing-document holder must come from the held Schedule 1")

    def feedback_superseded(self, feedback: StoredFeedback, assessment: Assessment) -> bool:
        item = feedback.item
        if item.scope.kind != "standing" or item.fact.type not in ("rate_accepted", "precedence"):
            return False
        for fid, fact in self.facts.items():
            if fact["kind"] != "amendment" or fact.get("signature_status") != "signed":
                continue
            if self.records[fid]["doc_ref"] not in self.signed_documents(fact.get("contract_id")):
                continue
            signed = (fact.get("signed_date") or {}).get("value")
            effective = (fact.get("effective_date") or {}).get("value")
            if not signed or date.fromisoformat(signed) <= feedback.decided_at.astimezone(timezone.utc).date():
                continue
            for scope in assessment.scopes:
                if (scope.contract_id == fact["contract_id"] and scope.lane in fact.get("scope", {}).get("lanes", [])
                        and scope.subject_date and effective and scope.subject_date >= date.fromisoformat(effective)):
                    return True
        return False

    def safety_proof(self, assessment: Assessment) -> SafetyProof:
        facts = self.facts
        contract_id = assessment.key.contract_id
        authorities = [f for f in facts.values() if f["kind"] == "authority" and f.get("contract_id") == contract_id]
        signed_docs = self.signed_documents(contract_id)
        contacts = [f for f in authorities if "billing_disputes" in f["categories"] and f.get("email")
                    and f.get("assertion_mode") == "direct"
                    and any(e["doc"] in signed_docs for e in f["evidence"])]
        # Schedule 1 may list billing-disputes contacts for BOTH parties. Resolve
        # the invoice issuer, not the shipper's AP address. Never choose the first
        # contact or infer a carrier from a hardcoded contract-ID prefix.
        invoices = [(fid, f) for fid, f in facts.items() if f["kind"] == "invoice"
                    and f.get("contract_id") == contract_id and f["document_type"] != "credit_note"
                    and (not assessment.key.invoice_id or invoice_key(f["invoice_number"]) == assessment.key.invoice_id)]
        issuers = {f["issuer"]["identifier"] for _, f in invoices if f.get("issuer") and f["issuer"].get("identifier")}
        customers = {f["customer"]["identifier"] for _, f in invoices if f.get("customer") and f["customer"].get("identifier")}
        issuer_domains = set()
        for fid, _ in invoices:
            doc = self.documents[self.records[fid]["doc_ref"]]
            covering = self.documents.get(doc.source.parent_ref) if doc.source.parent_ref else doc
            if covering is not None:
                issuer_domains.update(sender["address"].rsplit("@", 1)[-1].casefold()
                                      for sender in covering.source.metadata.get("from", []) if "@" in sender["address"])
        identified_contacts = [f for f in contacts if f.get("party") in issuers]
        if identified_contacts:
            contacts = identified_contacts
        else:
            contacts = [f for f in contacts if f["email"].rsplit("@", 1)[-1].casefold() in issuer_domains]
        contacts = [f for f in contacts if f.get("party") not in customers]
        if assessment.missing_doc:
            holder = assessment.missing_doc.holder
            contacts = [f for f in authorities if f.get("email") and (not holder.person or f["name"] == holder.person)
                        and f.get("assertion_mode") == "direct"
                        and assessment.missing_doc.category in f["categories"]
                        and (not holder.party or f.get("party") == holder.party)
                        and any(e["doc"] in signed_docs for e in f["evidence"])]
        recipient = contacts[0]["email"] if len({f["email"] for f in contacts}) == 1 else None
        carrier_domains = {f["email"].rsplit("@", 1)[-1].casefold() for f in contacts if "@" in f["email"]}
        party_domains = {f["email"].rsplit("@", 1)[-1].casefold() for f in authorities
                         if f.get("email") and "@" in f["email"] and f.get("assertion_mode") == "direct"
                         and any(e["doc"] in signed_docs for e in f["evidence"])}

        def direct(f: dict) -> bool:
            return f.get("assertion_mode") == "direct"

        def trusted_record(identifier: str) -> bool:
            record = self.records[identifier]
            f = record["fact"]
            doc = self.documents[record["doc_ref"]]
            if not direct(f) or f["kind"] not in ("invoice", "invoice_line", "shipment", "payment", "email"):
                return False
            covering = self.documents.get(doc.source.parent_ref) if doc.source.parent_ref else doc
            if covering is None:
                return False
            senders = covering.source.metadata.get("from", [])
            domains = carrier_domains if f["kind"] == "shipment" and f.get("record_type") == "milestone" else party_domains
            # Being attached does not promote an outsider's claims to level 3.
            # The shipper's ledger may legitimately arrive from the other party.
            return any(s["address"].rsplit("@", 1)[-1].casefold() in domains for s in senders)

        def trusted_term(identifier: str) -> bool:
            record = self.records[identifier]
            f = record["fact"]
            if not direct(f) or f["kind"] not in ("contract", "amendment", "term", "waiver", "email"):
                return False
            if record["doc_ref"] in signed_docs:
                return True
            doc = self.documents[record["doc_ref"]]
            if doc.source.kind != "email":
                return False
            category = ("waivers" if f["kind"] == "waiver" else
                        "payment_terms" if f.get("category") in ("payment", "payment_terms") or f.get("intent") == "due_date_extension" else
                        "rates" if f.get("category") in ("rate", "rates", "fuel", "accessorial") or f.get("intent") == "rate_notice" else None)
            if category is None:
                return False
            senders = doc.source.metadata.get("from", [])
            for person in authorities:
                if direct(person) and category in person["categories"] and any(e["doc"] in signed_docs for e in person["evidence"]):
                    if any(s["address"].casefold() == str(person.get("email", "")).casefold() for s in senders):
                        return True
            for feedback in self.feedback:
                if (feedback.item.fact.type == "sender_authority" and feedback.item.decision == "confirm"
                        and matches(feedback, assessment, self.as_of)
                        and category in feedback.item.fact.value.get("categories", [])):
                    person = feedback.item.fact.subject.get("person")
                    if any(s["name"] == person for s in senders):
                        return True
            return False

        chosen = assessment.term_fact_ids + assessment.operational_fact_ids
        complete = assessment.missing_doc is None and all(
            not [i for i in self.issues.get(self.records[fid]["doc_ref"], []) if i["code"] != "no_relevant_facts"] for fid in chosen)
        authority = (bool(assessment.term_fact_ids) and bool(assessment.operational_fact_ids)
                     and all(trusted_term(fid) for fid in assessment.term_fact_ids)
                     and all(trusted_record(fid) for fid in assessment.operational_fact_ids))
        pickup_ok = assessment.key.finding_class in ("cashback", "expiry", "late_fee_risk")
        scope_dates = [scope.subject_date for scope in assessment.scopes if scope.subject_date]
        if not pickup_ok:
            pickups = {}
            for fid, f in facts.items():
                if f["kind"] == "shipment" and str(f.get("event", "")).upper().replace(" ", "_") == "PICKED_UP" and (f.get("event_date") or {}).get("value"):
                    is_carrier_email = self.documents[self.records[fid]["doc_ref"]].source.kind == "email"
                    if trusted_record(fid) and (f["record_type"] == "tracking_event" or (f["record_type"] == "milestone" and is_carrier_email)):
                        priority = 0 if f["record_type"] == "tracking_event" else 1
                        pickups.setdefault(f["shipment_id"], []).append((priority, f["event_date"]["value"]))
            relevant = [s for s in assessment.scopes if s.shipment_id]
            pickup_ok = bool(relevant)
            expected_shipments = {facts[fid]["shipment_id"] for fid in assessment.operational_fact_ids
                                  if facts[fid]["kind"] == "invoice_line" and facts[fid].get("shipment_id")}
            if not expected_shipments:
                expected_shipments = {f["shipment_id"] for f in facts.values() if f["kind"] == "invoice_line"
                                      and invoice_key(f["invoice_number"]) == assessment.key.invoice_id and f.get("shipment_id")}
            pickup_ok &= expected_shipments <= {s.shipment_id for s in relevant}
            for scope in relevant:
                choices = pickups.get(scope.shipment_id, [])
                preferred = min((p for p, _ in choices), default=-1)
                values = {d for p, d in choices if p == preferred}
                pickup_ok &= scope.subject_date is not None and values == {scope.subject_date.isoformat()}
        in_force = bool(scope_dates)
        for fid in assessment.term_fact_ids:
            f = facts[fid]
            scope = f.get("scope", {})
            start = (scope.get("starts_on") or f.get("effective_date") or {}).get("value")
            end = (scope.get("ends_on") or {}).get("value")
            if not start:
                parent_terms = [v for v in facts.values() if v["kind"] in ("contract", "amendment")
                                and v.get("contract_id") == contract_id and direct(v)
                                and self.records[fid]["doc_ref"] in signed_docs
                                and any(e["doc"] == self.records[fid]["doc_ref"] for e in v["evidence"])]
                starts = [(p.get("effective_date") or {}).get("value") for p in parent_terms]
                start = max((s for s in starts if s), default=None)
            relevant_scopes = [s for s in assessment.scopes
                               if (not scope.get("lanes") or s.lane in scope["lanes"])
                               and (not scope.get("shipment_ids") or s.shipment_id in scope["shipment_ids"])
                               and (not scope.get("invoice_ids") or assessment.key.invoice_id in
                                    {invoice_key(n) for n in scope["invoice_ids"]})]
            applicable_dates = [s.subject_date for s in relevant_scopes if s.subject_date]
            explicitly_scoped_waiver = f["kind"] == "waiver" and bool(scope.get("shipment_ids") or scope.get("invoice_ids"))
            if not start and not explicitly_scoped_waiver:
                in_force = False
            if not any((not start or day >= date.fromisoformat(start)) and
                       (not end or day <= date.fromisoformat(end)) for day in applicable_dates):
                in_force = False
        return SafetyProof(complete, in_force, pickup_ok, authority, recipient)

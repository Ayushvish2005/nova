"""Worked-document calculations and host-side action/date/authority boundaries."""
from __future__ import annotations

import unittest
from datetime import date, timedelta
from decimal import Decimal

from grader.validate import citation_errors, schema_errors
from harness.audit_context import AuditContext
from harness.audit_models import Assessment, AssessmentBundle, MissingDocument
from harness.auditor import ClaudeAuditor, evaluation_groups
from harness.claude_extractor import canonical_json
from harness.facts import FactBundle
from harness.reporting import prepare, render
from test_reporting import feedback_record, proposal, stored
from test_structured_extraction import EXAMPLES, api_response, example_input, fact_payload, fake_client

CONTRACT = "TBX-US-EXP-E60"
INVOICE = "TX-US-2032-5394"
MSA_REF = "batch_01/0002.eml#MSA-TBX-US-EXP-E60.pdf"


def observed(value: str) -> dict:
    return {"raw": value, "value": value}


def worked_context() -> tuple[AuditContext, Assessment]:
    invoice = example_input("overcharge", "batch_01/0003.eml#TX-US-2032-5394.pdf")
    msa = example_input("overcharge", MSA_REF)
    tracking = example_input("overcharge", "batch_01/0005.eml#tracking-2031-07-01.xlsx")
    covering_email = example_input("overcharge", "batch_01/0003.eml")
    tracking_email = example_input("overcharge", tracking.document.source.parent_ref)
    payloads = {
        "invoice": fact_payload(invoice, kind="invoice", invoice_number=INVOICE, contract_id=CONTRACT,
                                document_type="invoice", invoice_date=observed("2032-01-02"), total="2095.29"),
        "freight": fact_payload(invoice, kind="invoice_line", invoice_number=INVOICE, description="Air freight",
                                shipment_id="TE56915687", lane="USE2-INE2", quantity="241", rate="6.69", amount="1612.29"),
        "fuel_line": fact_payload(invoice, kind="invoice_line", invoice_number=INVOICE, description="Fuel surcharge",
                                  shipment_id="TE56915687", lane="USE2-INE2", amount="325.04"),
        "contract": fact_payload(msa, page=3, kind="contract", contract_id=CONTRACT, signature_status="signed",
                                 effective_date=observed("2031-12-17")),
        "rate": fact_payload(msa, page=8, kind="term", contract_id=CONTRACT, category="rate", text="Freight rate",
                             scope={"lanes": ["USE2-INE2"], "starts_on": observed("2031-12-17"), "ends_on": observed("2032-06-16")},
                             values=[{"name": "rate", "raw": "6.01", "number": "6.01"}]),
        "fuel": fact_payload(msa, page=9, kind="term", contract_id=CONTRACT, category="fuel", text="December fuel",
                             scope={"starts_on": observed("2031-12-01"), "ends_on": observed("2031-12-31")},
                             values=[{"name": "percent", "raw": "20.16", "number": "20.16"}]),
        "contact": fact_payload(msa, page=11, kind="authority", contract_id=CONTRACT, name="Ravi Pembroke",
                                party="TBX-US", email="ravi.pembroke@tarrowby.example", categories=["billing_disputes"]),
        "pickup": fact_payload(tracking, kind="shipment", shipment_id="TE56915687", record_type="tracking_event",
                               event="PICKED_UP", event_date=observed("2031-12-25")),
    }
    records = {}
    for identifier, payload in payloads.items():
        fact = FactBundle.model_validate_json(canonical_json({"facts": [payload]})).facts[0].model_dump(mode="json")
        records[identifier] = {"fact_id": identifier, "doc_ref": fact["evidence"][0]["doc"], "fact": fact}
    context = AuditContext(date(2032, 8, 31), records,
                           {i.document.source.doc_ref: i.document for i in (invoice, msa, tracking, covering_email, tracking_email)}, [], {})

    def value(identifier, path):
        return {"op": "fact", "fact_id": identifier, "path": path}

    base = {"op": "round_money", "args": [{"op": "multiply", "args": [value("rate", "values.0.number"), value("freight", "quantity")]}]}
    fuel = {"op": "round_money", "args": [{"op": "divide", "args": [
        {"op": "multiply", "args": [base, value("fuel", "values.0.number")]}, {"op": "constant", "constant": "100"}]}]}
    assessment = proposal(INVOICE, key={"invoice_id": INVOICE, "contract_id": CONTRACT, "class": "overcharge"},
        amount={"op": "subtract", "args": [
            {"op": "add", "args": [value("freight", "amount"), value("fuel_line", "amount")]},
            {"op": "add", "args": [base, fuel]}]},
        term_fact_ids=["rate", "fuel"], operational_fact_ids=["freight", "fuel_line", "pickup"],
        evidence=[payloads[k]["evidence"][0] for k in ("freight", "rate", "fuel", "pickup")],
        scopes=[{"contract_id": CONTRACT, "lane": "USE2-INE2", "charge": "FRT", "shipment_id": "TE56915687",
                 "subject_date": "2031-12-25", "period": "P1", "period_end": "2032-06-16"}])
    return context, assessment


class WorkedSafetyTests(unittest.TestCase):
    def test_real_example_amount_authority_pickup_and_grader_citations(self) -> None:
        context, assessment = worked_context()
        items = prepare(AssessmentBundle(assessments=[assessment]), context)
        self.assertEqual(items[0].amount, Decimal("196.92"))
        self.assertTrue(all(vars(items[0].proof).values()))
        for mode, action in (("supervised", "draft:dispute"), ("autonomous", "execute:dispute")):
            report = render(items, context, mode, {})
            self.assertEqual(report["findings"][0]["action"], action)
            self.assertEqual(schema_errors(report), [])
            self.assertEqual(citation_errors(report, EXAMPLES / "overcharge/docs"), [])

    def test_unsigned_or_quoted_authority_and_pickup_gaps_hold_actions(self) -> None:
        for change in ("unsigned", "quoted_contact", "quoted_rate", "missing_pickup", "wrong_date", "issue"):
            with self.subTest(change=change):
                context, a = worked_context()
                if change == "unsigned":
                    context.records["contract"]["fact"]["signature_status"] = "unsigned"
                elif change.startswith("quoted_"):
                    context.records[change.removeprefix("quoted_")]["fact"]["assertion_mode"] = "quoted"
                elif change == "missing_pickup":
                    context.records["pickup"]["fact"]["record_type"] = "awb"
                elif change == "wrong_date":
                    a.scopes[0].subject_date = date(2031, 12, 26)
                else:
                    context.issues[MSA_REF] = [{"code": "ambiguous_rate", "description": "Unreadable band"}]
                report = render(prepare(AssessmentBundle(assessments=[a]), context), context, "autonomous", {})
                self.assertEqual(report["findings"][0]["action"], "human_review")

    def test_schedule_contacts_for_both_parties_do_not_target_shipper(self) -> None:
        context, a = worked_context()
        shipper_contact = dict(context.records["contact"]["fact"], name="Shipper AP",
                               party="QMP-US", email="ap@quillmere.example")
        context.records["shipper_contact"] = {"doc_ref": MSA_REF, "fact_id": "shipper_contact", "fact": shipper_contact}
        self.assertEqual(context.safety_proof(a).recipient, "ravi.pembroke@tarrowby.example")

    def test_every_affected_shipment_needs_pickup_support(self) -> None:
        context, a = worked_context()
        context.records["fuel_line"]["fact"]["shipment_id"] = "SECOND-SHIPMENT"
        self.assertFalse(context.safety_proof(a).pickup_supported)

    def test_wrong_invoice_lane_and_undeclared_calculation_are_rejected(self) -> None:
        context, a = worked_context()
        a.scopes[0].lane = "UNRELATED"
        with self.assertRaisesRegex(ValueError, "lane"):
            prepare(AssessmentBundle(assessments=[a]), context)
        context, a = worked_context()
        a.operational_fact_ids.remove("freight")
        with self.assertRaisesRegex(ValueError, "undeclared"):
            prepare(AssessmentBundle(assessments=[a]), context)

    def test_invoice_and_contract_alone_cannot_establish_conflict(self) -> None:
        context, a = worked_context()
        a.verdict = "conflict"
        a.conflict_amounts = [a.amount, a.amount]
        with self.assertRaisesRegex(ValueError, "authoritative deciding"):
            prepare(AssessmentBundle(assessments=[a]), context)

    def test_missing_document_report_uses_exact_named_gap_and_schedule_holder(self) -> None:
        # Test the reporting boundary, not whether a model should diagnose this
        # particular gap: the evaluator is responsible for that interpretation.
        context, a = worked_context()
        a.verdict, a.amount = "cannot_determine", None
        context.records["rates_contact"] = {"doc_ref": MSA_REF, "fact_id": "rates_contact", "fact":
            dict(context.records["contact"]["fact"], name="Yara Nairne", email="yara.nairne@tarrowby.example", categories=["rates"])}
        a.evidence = [FactBundle.model_validate_json(canonical_json({"facts": [
            fact_payload(example_input("overcharge", MSA_REF), page=3,
                         kind="contract", contract_id=CONTRACT)]})).facts[0].evidence[0]]
        a.missing_doc = MissingDocument.model_validate_json(canonical_json({
            "name": "Annex A", "holder": {"person": "Yara Nairne", "party": "TBX-US"}, "category": "rates"}))
        for mode, action in (("supervised", "draft:request_document"), ("autonomous", "human_review")):
            report = render(prepare(AssessmentBundle(assessments=[a]), context), context, mode, {})
            self.assertEqual(report["findings"][0]["action"], action)
            self.assertEqual(report["findings"][0]["missing_doc"]["name"], "Annex A")
            self.assertEqual(schema_errors(report), [])
        a.missing_doc.holder.person = "Ravi Pembroke"
        with self.assertRaisesRegex(ValueError, "Schedule 1"):
            context.validate_assessment(a)
        a.missing_doc.holder.person = "Yara Nairne"
        a.missing_doc.name = "Invented unavailable contract"
        with self.assertRaisesRegex(ValueError, "name must be stated"):
            context.validate_assessment(a)
        a.missing_doc.name, a.missing_doc.holder.person = "Annex A", "Body-only contact"
        with self.assertRaisesRegex(ValueError, "Schedule 1"):
            context.validate_assessment(a)

    def test_unresolved_standalone_email_remains_available_to_evaluator(self) -> None:
        context, _ = worked_context()
        email = example_input("overcharge", "batch_01/0004.eml")
        context.documents[email.document.source.doc_ref] = email.document
        payload = fact_payload(email, kind="email", intent="unknown", statement="Unresolved context")
        context.records["unresolved"] = {"fact_id": "unresolved", "doc_ref": email.document.source.doc_ref, "fact": payload}
        self.assertTrue(all("unresolved" in {r["fact_id"] for r in g["records"]} for g in evaluation_groups(context)))

    def test_claude_audit_adapter_repairs_invalid_output_and_uses_glossary(self) -> None:
        context, a = worked_context()
        payload = AssessmentBundle(assessments=[a]).model_dump(mode="json", by_alias=True)
        client = fake_client(api_response({"assessments": [{"bad": True}]}, name="propose_assessments"),
                             api_response(payload, name="propose_assessments"))
        result = ClaudeAuditor(client=client).assess(context)
        self.assertEqual(len(result.assessments), 1)
        self.assertEqual(client.messages.create.call_count, 2)
        request = client.messages.create.call_args.kwargs
        self.assertIn("GLOSSARY", request["system"])
        self.assertEqual(request["messages"][-1]["content"][0]["type"], "tool_result")

    def test_claude_cannot_silently_skip_an_invoice(self) -> None:
        context, _ = worked_context()
        client = fake_client(*[api_response({"assessments": []}, name="propose_assessments") for _ in range(2)])
        with self.assertRaisesRegex(ValueError, "every invoice target"):
            ClaudeAuditor(client=client).assess(context)

    def test_later_signed_amendment_supersedes_only_overlapping_feedback(self) -> None:
        context, a = worked_context()
        data = feedback_record()
        data["items"][0]["scope"]["applies_to"] = {"contract_id": CONTRACT, "lane": "USE2-INE2"}
        f = stored(data)
        context.records["amendment"] = {"doc_ref": MSA_REF, "fact_id": "amendment", "fact": {
            "kind": "amendment", "contract_id": CONTRACT, "signature_status": "signed",
            "assertion_mode": "direct",
            "signed_date": observed("2032-02-02"), "effective_date": observed("2031-12-20"),
            "scope": {"lanes": ["USE2-INE2"]}}}
        self.assertTrue(context.feedback_superseded(f, a))
        context.records["amendment"]["fact"]["scope"]["lanes"] = ["OTHER"]
        self.assertFalse(context.feedback_superseded(f, a))


class DateWindowTests(unittest.TestCase):
    def test_expiry_includes_zero_and_sixty_not_sixty_one_or_past(self) -> None:
        context, a = worked_context()
        a = a.model_copy(update={"key": proposal(cls="expiry", key={"class": "expiry", "contract_id": CONTRACT}, deadline="2032-08-31").key,
                                 "amount": None, "deadline": context.as_of})
        for delta in (-1, 0, 60, 61):
            a.deadline = context.as_of + timedelta(days=delta)
            items = prepare(AssessmentBundle(assessments=[a]), context)
            self.assertEqual(len(items), int(delta in (0, 60)))

    def test_late_risk_includes_one_and_fourteen_not_due_or_fifteen(self) -> None:
        context, a = worked_context()
        a.key.finding_class = "late_fee_risk"
        for delta in (0, 1, 14, 15):
            a.deadline = context.as_of + timedelta(days=delta)
            items = prepare(AssessmentBundle(assessments=[a]), context)
            self.assertEqual(len(items), int(delta in (1, 14)))
        a.deadline = context.as_of + timedelta(days=1)
        context.records["paid"] = {"doc_ref": MSA_REF, "fact": {
            "kind": "payment", "invoice_number": INVOICE, "status": "paid", "paid_date": observed("2032-08-31")}}
        self.assertEqual(prepare(AssessmentBundle(assessments=[a]), context), [])

    def test_cashback_requires_period_closed_and_tracks_until_due(self) -> None:
        context, a = worked_context()
        a = a.model_copy(update={"key": proposal(cls="cashback", key={"class": "cashback", "contract_id": CONTRACT,
            "period": "2032-Q2", "credit_type": "rebate"}, period_end="2032-06-30").key,
            "period_end": context.as_of, "credit_due_date": context.as_of + timedelta(days=1)})
        self.assertEqual(prepare(AssessmentBundle(assessments=[a]), context), [])
        a.period_end -= timedelta(days=1)
        items = prepare(AssessmentBundle(assessments=[a]), context)
        self.assertEqual(render(items, context, "autonomous", {})["portfolio"]["cashback"][0]["action"], "internal:track_credit")
        context.as_of += timedelta(days=1)
        items = prepare(AssessmentBundle(assessments=[a]), context)
        self.assertEqual(render(items, context, "supervised", {})["portfolio"]["cashback"][0]["action"], "draft:claim_credit")

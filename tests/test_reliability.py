"""Unseen fictional inputs and regressions independent of public answer files."""
from __future__ import annotations

import copy
import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from grader.validate import citation_errors, schema_errors
from harness.audit_context import AuditContext
from harness.audit_models import Assessment, AssessmentBundle
from harness.auditor import compact_claim, evaluation_groups
from harness.claude_extractor import ClaudeExtractor, ExtractionInput, StructuredExtractionError, canonical_json
from harness.codex_client import CodexClient, strict_schema
from harness.facts import FactBundle
from harness.feedback import matches
from harness.models import Document
from harness.readers import DocumentReader, message_sources
from harness.reporting import prepare, render
from test_report_safety import worked_context, observed
from test_reporting import feedback_record, proposal, stored
from test_structured_extraction import fake_client, api_response

ROOT = Path(__file__).resolve().parents[1]
CUSTOM = ROOT / "evals/mailboxes"


def custom_context(name: str, as_of: str = "2039-02-20") -> tuple[AuditContext, Assessment]:
    reader = DocumentReader()
    documents = {}
    for batch in sorted((CUSTOM / name).glob("batch_*")):
        for path in sorted(batch.glob("*.eml")):
            for source, raw in message_sources(batch, path):
                if source.received_at[:10] <= as_of:
                    documents[source.doc_ref] = Document(source, reader.cache_key, reader.extract(raw, source.format))
    records = {}

    def add(identifier: str, doc_ref: str, **fields):
        page = documents[doc_ref].extraction.pages[0]
        payload = {"confidence": 0.95, "assertion_mode": "direct", "evidence": [{"doc": doc_ref, "page": 1,
                   "quote": page.text, "layer": page.method if page.method == "ocr" else "native"}], **fields}
        fact = FactBundle.model_validate_json(canonical_json({"facts": [payload]})).facts[0].model_dump(mode="json")
        records[identifier] = {"fact_id": identifier, "doc_ref": doc_ref, "fact": fact}

    msa = "batch_01/0001.eml#services.pdf"
    invoice = "batch_01/0003.eml#account-advice.pdf"
    add("contract", msa, kind="contract", contract_id="OC-RAIL-39", signature_status="signed", effective_date=observed("2039-01-01"))
    add("contact", msa, kind="authority", contract_id="OC-RAIL-39", name="Noor Finch", party="OC",
        email="disputes@orchid.example", categories=["billing_disputes"])
    add("invoice", invoice, kind="invoice", invoice_number="ALT-#39:07", document_type="invoice", contract_id="OC-RAIL-39",
        total="33.00", invoice_date=observed("2039-01-20"))
    add("freight", invoice, kind="invoice_line", invoice_number="ALT-#39:07", description="Base linehaul",
        shipment_id="Z9-001", lane="PORT-RIDGE", quantity="10.0", rate="3.00", amount="30.00")
    add("fuel_line", invoice, kind="invoice_line", invoice_number="ALT-#39:07", description="Energy adjustment",
        shipment_id="Z9-001", lane="PORT-RIDGE", amount="3.00")
    add("pickup", "batch_01/0002.eml", kind="shipment", shipment_id="Z9-001", record_type="milestone", event="PICKED_UP",
        event_date=observed("2039-01-19"))
    add("rate", msa, kind="term", contract_id="OC-RAIL-39", category="rate", text="Base linehaul tariff",
        scope={"lanes": ["PORT-RIDGE"]}, values=[{"name": "rate", "raw": "3.00" if name == "backdated" else "2.00",
                                                  "number": "3.00" if name == "backdated" else "2.00"}])
    add("fuel", msa, kind="term", contract_id="OC-RAIL-39", category="fuel", text="Energy adjustment equals 10%",
        values=[{"name": "percent", "raw": "10%", "number": "10"}])
    amendment = "batch_02/0001.eml#retroactive-amendment.pdf"
    if amendment in documents:
        add("amendment", amendment, kind="amendment", amendment_id="OC-RAIL-39/AMD-1", contract_id="OC-RAIL-39",
            signature_status="signed", signed_date=observed("2039-02-04"), effective_date=observed("2039-01-15"),
            scope={"lanes": ["PORT-RIDGE"]}, changes="Replace Annex A rate with USD 2.00")
        add("rate", amendment, kind="term", contract_id="OC-RAIL-39", category="rate", text="Replacement base linehaul tariff",
            scope={"lanes": ["PORT-RIDGE"]}, values=[{"name": "rate", "raw": "2.00", "number": "2.00"}])
    _, original = worked_context()
    a = original.model_dump(mode="json", by_alias=True)
    a.update(key={"invoice_id": "ALT-3907", "contract_id": "OC-RAIL-39", "class": "overcharge"},
             evidence=[records[k]["fact"]["evidence"][0] for k in ("freight", "rate", "fuel", "pickup")],
             scopes=[{"contract_id": "OC-RAIL-39", "lane": "PORT-RIDGE", "charge": "base linehaul",
                      "shipment_id": "Z9-001", "subject_date": "2039-01-19"}])
    return AuditContext(date.fromisoformat(as_of), records, documents, [], {}), Assessment.model_validate_json(canonical_json(a))


class CustomMailboxTests(unittest.TestCase):
    def test_unseen_layout_and_carrier_recovery_and_citations(self):
        context, a = custom_context("alternate_layout")
        report = render(prepare(AssessmentBundle(assessments=[a]), context), context, "autonomous", {})
        self.assertEqual(report["findings"][0]["amount_usd"], "11.00")
        self.assertEqual(report["findings"][0]["action"], "execute:dispute")
        self.assertEqual(citation_errors(report, CUSTOM / "alternate_layout"), [])
        self.assertEqual(schema_errors(report), [])

    def test_real_scan_is_ocr_and_keeps_verifiable_source_identity(self):
        context, a = custom_context("scanned")
        page = context.documents["batch_01/0003.eml#account-advice.pdf"].extraction.pages[0]
        self.assertEqual(page.native_text.strip(), "")
        self.assertEqual(page.method, "ocr")
        self.assertIn("33.00", page.text)
        report = render(prepare(AssessmentBundle(assessments=[a]), context), context, "supervised", {})
        self.assertEqual(report["findings"][0]["amount_usd"], "11.00")
        self.assertEqual(citation_errors(report, CUSTOM / "scanned"), [])

    def test_backdated_arrival_revises_amount_and_sets_lineage(self):
        before, a = custom_context("backdated", "2039-01-31")
        old = render(prepare(AssessmentBundle(assessments=[a]), before), before, "supervised", {})["findings"][0]
        self.assertEqual(old["verdict"], "no_finding")
        after, a = custom_context("backdated")
        items = prepare(AssessmentBundle(assessments=[a]), after)
        new = render(items, after, "supervised", {a.key.identity(): old})["findings"][0]
        self.assertEqual(new["amount_usd"], "11.00")
        self.assertEqual(new["supersedes"], [old["id"]])
        first_seen = render(items, after, "supervised", {})["findings"][0]
        self.assertTrue(first_seen["retro"])
        self.assertEqual(first_seen["supersedes"], [])
        self.assertTrue(any("retroactive-amendment.pdf" in e["doc"] for e in first_seen["evidence"]))

    def test_injection_is_data_and_cannot_add_action_fields(self):
        context, _ = custom_context("injection")
        doc = context.documents["batch_01/0004.eml"]
        payload = {"facts": [], "issues": [{"code": "no_relevant_facts", "description": "Untrusted instructions only"}]}
        bad = dict(payload, actions=["execute:pay"])
        client = fake_client(api_response(bad), api_response(payload))
        result = ClaudeExtractor(client=client).extract(ExtractionInput(doc, [{"doc_ref": doc.source.doc_ref}]))
        self.assertEqual(result.bundle.facts, [])
        self.assertEqual(client.messages.create.call_count, 2)
        request = client.messages.create.call_args.kwargs
        self.assertIn("untrusted DATA", request["system"])
        self.assertIn("SYSTEM OVERRIDE", request["messages"][0]["content"])


class SafetyRegressionTests(unittest.TestCase):
    def test_outsider_attachment_is_not_promoted_to_operational_authority(self):
        context, a = worked_context()
        ref = context.documents[context.records["pickup"]["doc_ref"]].source.parent_ref
        covering = context.documents[ref]
        context.documents[ref] = replace(covering, source=replace(covering.source,
            metadata={**covering.source.metadata, "from": [{"name": "Outside vendor", "address": "tracking@outsider.example"}]}))
        proof = context.safety_proof(a)
        self.assertFalse(proof.authority_supported)
        self.assertFalse(proof.pickup_supported)

    def test_large_contract_partitions_targets_not_historical_evidence(self):
        context, _ = worked_context()
        for index in range(10):
            record = copy.deepcopy(context.records["invoice"])
            record["fact"]["invoice_number"] = f"UNSEEN-{index}"
            record["fact_id"] = f"invoice-{index}"
            context.records[f"invoice-{index}"] = record
        groups = evaluation_groups(context)
        targets = [target for group in groups for target in group["invoice_targets"]]
        self.assertEqual(len(targets), 11)
        self.assertEqual(len(set(targets)), 11)
        self.assertTrue(all(len(group["invoice_targets"]) <= 4 for group in groups))
        self.assertEqual(sum(bool(group["contract_targets"]) for group in groups), 1)
        for group in groups:
            self.assertTrue(set(context.records) <= {r["fact_id"] for r in group["records"]})

    def test_context_compaction_preserves_zero_and_array_paths(self):
        result = compact_claim({"unused": None, "values": [{"number": "0", "date_value": None}, {}, None],
                                "amount": 0, "flag": False, "empty": []})
        self.assertEqual(result["values"], [{"number": "0"}, {}, None])
        self.assertEqual(result["amount"], 0)
        self.assertFalse(result["flag"])
        self.assertNotIn("unused", result)

    def test_split_signature_and_effective_date_same_held_instrument(self):
        context, a = worked_context()
        header = context.records["contract"]["fact"]
        signature = copy.deepcopy(header)
        header["signature_status"] = "unknown"
        signature["effective_date"] = None
        context.records["signature"] = {"doc_ref": context.records["contract"]["doc_ref"], "fact": signature}
        self.assertTrue(context.safety_proof(a).terms_in_force)

    def test_invoice_registration_identifier_does_not_hide_schedule_contact(self):
        context, a = worked_context()
        context.records["invoice"]["fact"]["issuer"] = {"name": "Carrier", "identifier": "REGISTRATION-999"}
        self.assertEqual(context.safety_proof(a).recipient, "ravi.pembroke@tarrowby.example")

    def test_two_month_terms_apply_to_their_own_shipment_dates(self):
        context, a = worked_context()
        second = copy.deepcopy(context.records["fuel"])
        second["fact"]["scope"].update(starts_on=observed("2032-01-01"), ends_on=observed("2032-01-31"))
        context.records["january_fuel"] = second
        a.term_fact_ids.append("january_fuel")
        next_scope = a.scopes[0].model_copy(deep=True)
        next_scope.subject_date = date(2032, 1, 10)
        a.scopes.append(next_scope)
        self.assertTrue(context.safety_proof(a).terms_in_force)
        context.records["january_fuel"]["fact"]["scope"]["lanes"] = ["UNRELATED"]
        self.assertFalse(context.safety_proof(a).terms_in_force)

    def test_one_off_confirmation_uses_subject_date_not_report_date(self):
        data = feedback_record(scope_kind="one_off")
        data["items"][0]["scope"].update(valid_from="2032-01-01", valid_to="2032-01-31")
        self.assertTrue(matches(stored(data), proposal(), date(2032, 2, 20)))
        other = proposal()
        other.scopes[0].subject_date = date(2032, 2, 1)
        self.assertFalse(matches(stored(data), other, date(2032, 2, 20)))


class CodexBoundaryTests(unittest.TestCase):
    def test_sdk_provider_bounds_dense_input_without_discarding_source_text(self):
        from harness.providers import extraction_provider

        with patch.dict("os.environ", {"HARNESS_BACKEND": "codex"}):
            provider = extraction_provider()
        self.assertEqual(provider.chunk_chars, 1800)
        self.assertEqual(provider.chunk_pages, 1)
        self.assertEqual(provider.chunk_workers, 3)
        context, _ = custom_context("alternate_layout")
        document = context.documents["batch_01/0001.eml#services.pdf"]
        page = replace(document.extraction.pages[0], text="dense clause\n" * 600, native_text="dense clause\n" * 600)
        document = replace(document, extraction=replace(document.extraction, pages=[page]))
        chunks = provider.chunks(ExtractionInput(document, []))
        segments = [segment for chunk in chunks for segment in chunk["primary"]]
        self.assertEqual("".join(segment["text"] for segment in segments), page.text)
        self.assertTrue(all(len(segment["text"]) <= 1800 for segment in segments))
        self.assertEqual({segment["page"] for segment in segments}, {1})

    def test_strict_schema_closes_objects_but_keeps_nullable_fields(self):
        schema = strict_schema(FactBundle.model_json_schema())
        contract = schema["$defs"]["Contract"]
        self.assertEqual(set(contract["required"]), set(contract["properties"]))
        self.assertFalse(contract["additionalProperties"])
        self.assertIn({"type": "null"}, contract["properties"]["effective_date"]["anyOf"])

    def sdk_fixture(self, *, items=None, final_response='{"value": 7}'):
        completed = SimpleNamespace(id="sdk-turn", status="completed", error=None, final_response=final_response,
                                    items=items or [], usage=SimpleNamespace(last=SimpleNamespace(input_tokens=10, output_tokens=5)))
        turn = SimpleNamespace(run=AsyncMock(return_value=completed), interrupt=AsyncMock())
        thread = SimpleNamespace(turn=AsyncMock(return_value=turn))
        sdk = SimpleNamespace(thread_start=AsyncMock(return_value=thread), close=AsyncMock())
        return sdk, thread, turn

    def test_sdk_is_isolated_and_rejects_tool_activity(self):
        tool = {"name": "return_data", "input_schema": {"type": "object", "properties": {"value": {"type": "integer"}}}}
        sdk, thread, _ = self.sdk_fixture()
        with patch("harness.codex_client.AsyncCodex", return_value=sdk) as factory:
            client = CodexClient()
            result = client.create(system="Extract data", messages=[{"role": "user", "content": "value=7"}], tools=[tool])
            self.assertEqual(result.content[0].input, {"value": 7})
            options = sdk.thread_start.call_args.kwargs
            self.assertEqual(options["sandbox"].value, "read-only")
            self.assertEqual(options["approval_mode"].value, "deny_all")
            self.assertTrue(options["ephemeral"])
            self.assertNotEqual(Path(options["cwd"]), ROOT)
            self.assertNotIn("value=7", options["base_instructions"])
            self.assertEqual(thread.turn.call_args.args[0].tool_name, "source_documents")
            self.assertIn("value=7", thread.turn.call_args.args[0].content)
            self.assertIn("features.shell_tool=false", factory.call_args.kwargs["config"].config_overrides)
            sdk.close.assert_awaited_once()
        sdk, _, _ = self.sdk_fixture(items=[SimpleNamespace(type="commandExecution")])
        with patch("harness.codex_client.AsyncCodex", return_value=sdk):
            with self.assertRaisesRegex(StructuredExtractionError, "non-data operation"):
                client.create(system="Extract", messages=[], tools=[tool])

    def test_sdk_timeout_interrupts_turn_and_closes_runtime(self):
        sdk, _, turn = self.sdk_fixture()

        async def pending():
            await asyncio.Event().wait()

        turn.run.side_effect = pending
        with patch("harness.codex_client.AsyncCodex", return_value=sdk):
            with self.assertRaisesRegex(StructuredExtractionError, "timed out"):
                CodexClient(timeout=0.02).create(system="Extract", messages=[], tools=[{
                    "name": "return_data", "input_schema": {"type": "object", "properties": {}}}])
        turn.interrupt.assert_awaited_once()
        sdk.close.assert_awaited_once()

    def test_sdk_cache_validates_and_separates_inputs(self):
        tool = {"name": "return_data", "input_schema": {"type": "object", "properties": {"value": {"type": "integer"}}}}
        sdk, _, turn = self.sdk_fixture()
        with tempfile.TemporaryDirectory() as cache, patch.dict("os.environ", {"CODEX_CACHE_DIR": cache}), \
                patch("harness.codex_client.AsyncCodex", return_value=sdk):
            client = CodexClient()
            one = client.create(system="Extract", messages=[{"content": "one"}], tools=[tool])
            self.assertEqual(client.create(system="Extract", messages=[{"content": "one"}], tools=[tool]), one)
            self.assertEqual(turn.run.await_count, 1)
            client.create(system="Extract", messages=[{"content": "two"}], tools=[tool])
            self.assertEqual(turn.run.await_count, 2)
            for path in Path(cache).glob("*.json"):
                path.write_text("invalid JSON")
            client.create(system="Extract", messages=[{"content": "one"}], tools=[tool])
            self.assertEqual(turn.run.await_count, 3)

    def test_sdk_default_configuration_changes_invalidate_cache_identity(self):
        with tempfile.TemporaryDirectory() as config_dir, patch.dict("os.environ", {"CODEX_HOME": config_dir}):
            config = Path(config_dir) / "config.toml"
            config.write_text('model = "configured-first"\nmodel_reasoning_effort = "low"\n')
            first = CodexClient().base_url
            self.assertEqual(CodexClient().base_url, first)
            config.write_text('model = "configured-second"\nmodel_reasoning_effort = "high"\n')
            self.assertNotEqual(CodexClient().base_url, first)

    def test_sdk_external_input_echo_allowed_but_other_tool_outputs_rejected(self):
        messages = [{"content": "value=7"}]
        echo = SimpleNamespace(type="functionCallOutput", name="source_documents", namespace=None,
                               output=SimpleNamespace(root=canonical_json(messages)))
        sdk, _, _ = self.sdk_fixture(items=[echo])
        tool = {"name": "return_data", "input_schema": {"type": "object", "properties": {"value": {"type": "integer"}}}}
        with patch("harness.codex_client.AsyncCodex", return_value=sdk):
            self.assertEqual(CodexClient().create(system="Extract", messages=messages, tools=[tool]).content[0].input, {"value": 7})
            echo.output.root = "foreign output"
            with self.assertRaisesRegex(StructuredExtractionError, "non-data operation"):
                CodexClient().create(system="Extract", messages=messages, tools=[tool])

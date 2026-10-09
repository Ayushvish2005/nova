"""Deterministic feedback, money, safety, schema, lineage and CLI tests."""
from __future__ import annotations

import copy
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from grader.validate import citation_errors, schema_errors
from harness.actions import PreparedFinding, SafetyProof, select_actions
from harness.audit_context import AuditContext
from harness.audit_models import Assessment, AssessmentBundle, Expression
from harness.calculations import evaluate, money
from harness.claude_extractor import StructuredResult, canonical_json
from harness.fact_store import FactStore
from harness.facts import FactBundle
from harness.feedback import StoredFeedback, ingest_feedback, load_feedback, matches
from harness.harness import main
from harness.reporting import build_report, prepare
from harness.structured_ingestion import extract_pending

import test_structured_extraction as fixtures
from test_structured_extraction import EXAMPLES, FixtureExtractor, example_input, fact_payload

REF = "batch_01/0003.eml#TX-US-2032-5394.pdf"


def proposal(invoice: str = "INV-1", cls: str = "overcharge", verdict: str = "finding", **changes) -> Assessment:
    data = {
        "key": {"class": cls, "invoice_id": invoice, "contract_id": "CONTRACT-1"},
        "verdict": verdict, "rationale": "Compare the billed line with the stated term",
        "evidence": [{"doc": REF, "page": 1, "quote": "Invoice date 2032-01-02 Due date 2032-02-16"}],
        "confidence": 0.9, "amount": {"op": "fact", "fact_id": "line", "path": "amount"},
        "scopes": [{"contract_id": "CONTRACT-1", "lane": "A-B", "charge": "FRT", "person": "Ada",
                    "category": "rates", "subject_date": "2032-01-02", "period": "P1", "period_end": "2032-06-30"}],
        "operational_fact_ids": ["line"], "term_fact_ids": ["term"],
        **changes,
    }
    return Assessment.model_validate_json(canonical_json(data))


def feedback_record(decision: str = "confirm", *, scope_kind: str = "standing", **changes) -> dict:
    return {
        "schema_version": "1.0", "world": "W-TEST", "after_batch": "batch_01",
        "decided_at": "2032-02-01T10:00:00Z", "decided_by": {"name": "AP Manager", "entity": "New Customer"},
        "items": [{
            "item_id": "fb-1", "finding_key": {"invoice_id": "INV-1", "class": "overcharge"},
            "decision": decision, "fact": {"type": "rate_accepted", "subject": {"contract_id": "CONTRACT-1", "lane": "A-B"},
                                           "value": {"source": "notice"}, "evidence": "Confirmed with carrier"},
            "scope": {"kind": scope_kind,
                      "applies_to": {"contract_id": "CONTRACT-1", "lane": "A-B"} if scope_kind == "standing" else {"invoice_id": "INV-1"},
                      "valid_from": "2032-01-01", "valid_to": "2032-06-30"}, **changes,
        }],
    }


def stored(envelope: dict) -> StoredFeedback:
    return StoredFeedback.model_validate_json(canonical_json({
        "world": envelope["world"], "after_batch": envelope["after_batch"], "decided_at": envelope["decided_at"], "item": envelope["items"][0],
    }))


class FeedbackScopeTests(unittest.TestCase):
    def test_standing_requires_every_filter_and_subject_date(self) -> None:
        f = stored(feedback_record())
        self.assertTrue(matches(f, proposal(invoice="LATER-INVOICE"), date(2032, 2, 2)))
        for field, value in (("contract_id", "OTHER"), ("lane", "C-D"), ("subject_date", date(2032, 7, 1)), ("subject_date", None)):
            a = proposal(invoice="LATER-INVOICE")
            setattr(a.scopes[0], field, value)
            self.assertFalse(matches(f, a, date(2032, 2, 2)), field)
        self.assertFalse(matches(f, proposal(), date(2032, 1, 31)))

    def test_one_off_is_exact_key_not_other_invoice_or_class(self) -> None:
        f = stored(feedback_record("one_off_approval", scope_kind="one_off", fact={
            "type": "waiver_scope", "subject": {"person": "Ada"}, "value": {"months": ["2032-01"]}, "evidence": "Confirmed by AP"}))
        self.assertTrue(matches(f, proposal(), date(2032, 2, 2)))
        self.assertFalse(matches(f, proposal(invoice="INV-2"), date(2032, 2, 2)))
        self.assertFalse(matches(f, proposal(cls="duplicate"), date(2032, 2, 2)))
        wrong_month = proposal()
        wrong_month.scopes[0].subject_date = date(2032, 4, 1)
        self.assertFalse(matches(f, wrong_month, date(2032, 2, 2)))

    def test_close_named_key_even_when_standing_rate_filters_do_not_match(self) -> None:
        f = stored(feedback_record("close"))
        a = proposal()
        a.scopes = []
        self.assertTrue(matches(f, a, date(2032, 2, 2)))
        # The accepted standing rate can affect a later invoice; prepare() still
        # closes only finding_key, never every matching standing claim.
        self.assertTrue(matches(f, proposal(invoice="INV-2"), date(2032, 2, 2)))

    def test_authority_does_not_transfer_to_other_person_or_category(self) -> None:
        data = feedback_record(fact={"type": "sender_authority", "subject": {"person": "Ada"},
                                     "value": {"categories": ["waivers"]}, "evidence": "Delegation checked"})
        data["items"][0]["scope"]["applies_to"].update(person="Ada", category="waivers")
        f = stored(data)
        a = proposal()
        a.scopes[0].category = "waivers"
        self.assertTrue(matches(f, a, date(2032, 2, 2)))
        a.scopes[0].person = "Someone Else"
        self.assertFalse(matches(f, a, date(2032, 2, 2)))
        a.scopes[0].person, a.scopes[0].category = "Ada", "rates"
        self.assertFalse(matches(f, a, date(2032, 2, 2)))

    def test_unknown_filter_fails_closed(self) -> None:
        data = feedback_record()
        data["items"][0]["scope"]["applies_to"]["new_unrecognized_filter"] = "x"
        self.assertFalse(matches(stored(data), proposal(), date(2032, 2, 2)))

    def test_feedback_idempotent_and_changed_id_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path, state = Path(root) / "feedback.json", Path(root) / "state"
            data = feedback_record()
            path.write_text(canonical_json(data))
            self.assertEqual(ingest_feedback(path, state), 1)
            self.assertEqual(ingest_feedback(path, state), 0)
            changed = copy.deepcopy(data["items"][0])
            changed["item_id"] = "fb-new"
            data["items"].insert(0, changed)
            data["items"][1]["note"] = "changed"
            path.write_text(canonical_json(data))
            with self.assertRaisesRegex(ValueError, "changed"):
                ingest_feedback(path, state)
            with FactStore(state) as store:
                self.assertEqual(store.connection.execute("SELECT count(*) FROM feedback_items").fetchone()[0], 1)
                self.assertEqual(load_feedback(store, date(2032, 1, 31)), [])
                self.assertEqual(len(load_feedback(store, date(2032, 2, 2))), 1)

    def test_existing_feedback_file_is_accepted_without_reinterpretation(self) -> None:
        path = EXAMPLES.parents[0] / "scenarios/feedback/W1_batch_01.json"
        with tempfile.TemporaryDirectory() as state:
            self.assertEqual(ingest_feedback(path, state), 1)
            with FactStore(state) as store:
                f = load_feedback(store, date(2035, 6, 30))[0]
                self.assertEqual(f.item.scope.applies_to["lane"], "USC3-INS1")
                self.assertFalse(f.item.solicited)


class CalculationTests(unittest.TestCase):
    def test_half_up_rounding_and_inr_conversion_once(self) -> None:
        self.assertEqual(money(Decimal("1.005")), "1.01")
        self.assertEqual(money(Decimal("-1.005")), "-1.01")
        data = {"invoice": {"amount": "83.83"}, "fx": {"rate": "83"}}
        expr = Expression.model_validate_json(canonical_json({"op": "round_money", "args": [{
            "op": "divide", "args": [{"op": "fact", "fact_id": "invoice", "path": "amount"},
                                       {"op": "fact", "fact_id": "fx", "path": "rate"}]}]}))
        self.assertEqual(evaluate(expr, data), Decimal("1.01"))

    def test_business_constants_and_arbitrary_code_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            evaluate(Expression(op="constant", constant="4999"), {})
        with self.assertRaises(ValueError):
            evaluate(Expression(op="fact", fact_id="x", path="confidence"), {"x": {"confidence": 1}})
        with self.assertRaises(ValueError):
            evaluate(Expression(op="fact", fact_id="x", path="amount"), {"x": {"amount": "__import__('os')"}})

    def test_ceil_months_and_source_date_difference(self) -> None:
        data = {"ledger": {"paid": "2032-03-04", "due": "2032-02-01"}}
        days = {"op": "days_between", "args": [{"op": "fact", "fact_id": "ledger", "path": "paid"},
                                                 {"op": "fact", "fact_id": "ledger", "path": "due"}]}
        expr = Expression.model_validate_json(canonical_json({"op": "ceil", "args": [{"op": "divide", "args": [days, {"op": "constant", "constant": "30"}]}]}))
        self.assertEqual(evaluate(expr, data), Decimal(2))


class ActionTests(unittest.TestCase):
    def item(self, invoice="INV-1", cls="overcharge", amount="100.00") -> PreparedFinding:
        return PreparedFinding(proposal(invoice, cls), Decimal(amount), proof=SafetyProof(True, True, True, True, "disputes@carrier.example"))

    def test_supervised_never_executes_and_autonomous_needs_every_gate(self) -> None:
        item = self.item()
        self.assertEqual(select_actions([item], "supervised", date(2032, 2, 1))[item.assessment.key.identity()], "draft:dispute")
        self.assertEqual(select_actions([item], "autonomous", date(2032, 2, 1))[item.assessment.key.identity()], "execute:dispute")
        for field in ("evidence_complete", "terms_in_force", "pickup_supported", "authority_supported", "recipient"):
            item = self.item()
            setattr(item.proof, field, None if field == "recipient" else False)
            self.assertEqual(select_actions([item], "autonomous", date(2032, 2, 1))[item.assessment.key.identity()], "human_review", field)

    def test_threshold_aggregates_classes_on_same_invoice_and_includes_exact_5000(self) -> None:
        first, second = self.item(amount="3000"), self.item(cls="duplicate", amount="2000")
        for mode in ("supervised", "autonomous"):
            self.assertEqual(set(select_actions([first, second], mode, date(2032, 2, 1)).values()), {"human_review"})
        second.assessment.key.invoice_id = "INV-2"
        self.assertEqual(set(select_actions([first, second], "autonomous", date(2032, 2, 1)).values()), {"execute:dispute"})

    def test_conflicts_block_only_overlapping_lane_charge_and_period(self) -> None:
        item = self.item()
        a = proposal("INV-2", verdict="conflict", conflict_amounts=[{"op": "constant", "constant": "0"}] * 2,
                     evidence=[{"doc": REF, "page": 1, "quote": "Invoice date 2032-01-02 Due date 2032-02-16"}] * 2)
        conflict = PreparedFinding(a)
        self.assertEqual(select_actions([item, conflict], "autonomous", date(2032, 2, 1))[item.assessment.key.identity()], "human_review")
        a.scopes[0].period = "different_label_same_dates"
        self.assertEqual(select_actions([item, conflict], "autonomous", date(2032, 2, 1))[item.assessment.key.identity()], "human_review")
        a.scopes[0].period = "P1"
        for field, value in (("lane", "OTHER"), ("charge", "DLV"), ("subject_date", date(2032, 7, 1)), ("contract_id", "OTHER")):
            old = getattr(a.scopes[0], field)
            setattr(a.scopes[0], field, value)
            actions = select_actions([item, conflict], "autonomous", date(2032, 2, 1))
            self.assertEqual(actions[item.assessment.key.identity()], "execute:dispute", field)
            self.assertEqual(actions[a.key.identity()], "escalate")
            setattr(a.scopes[0], field, old)

    def test_internal_actions_and_undue_credit_do_not_execute_outbound(self) -> None:
        expiry = proposal(cls="expiry", deadline="2032-03-01")
        risk = proposal(cls="late_fee_risk", deadline="2032-02-10")
        credit = proposal(cls="cashback", key={"class": "cashback", "contract_id": "CONTRACT-1", "period": "2031-Q4", "credit_type": "rebate"},
                          period_end="2031-12-31", credit_due_date="2032-02-15")
        items = [PreparedFinding(a, Decimal("10")) for a in (expiry, risk, credit)]
        self.assertEqual(set(select_actions(items, "autonomous", date(2032, 2, 1)).values()),
                         {"internal:alert", "internal:payment_reminder", "internal:track_credit"})

    def test_one_off_approval_does_not_bypass_safety(self) -> None:
        item = self.item()
        item.learned_at = {"feedback_item_id": "approval", "batch": "batch_01"}
        item.proof.authority_supported = False
        self.assertEqual(select_actions([item], "autonomous", date(2032, 2, 1))[item.assessment.key.identity()], "human_review")


class ReportingExtractor(FixtureExtractor):
    def extract(self, item):
        self.calls.append(item.document.source.doc_ref)
        if item.document.source.format == "pdf":
            fact = fact_payload(item, kind="invoice", invoice_number="TX-US-2032-5394", document_type="invoice",
                                contract_id="TBX-US-EXP-E60", total="2095.29", invoice_date={"raw": "2032-01-02", "value": "2032-01-02"})
        else:
            fact = fact_payload(item, kind="email", intent="invoice_sent", statement="Invoice supplied")
        return StructuredResult(FactBundle.model_validate_json(canonical_json({"facts": [fact]})))


class FakeAuditor:
    cache_key = "test-auditor:1"

    def __init__(self):
        self.calls = 0
        self.factor = "1"

    def assess(self, context):
        self.calls += 1
        fid = next(fid for fid, f in context.facts.items() if f["kind"] == "invoice")
        a = proposal("TX-US-2032-5394", key={"class": "overcharge", "invoice_id": "TX-US-2032-5394", "contract_id": "TBX-US-EXP-E60"},
                     amount={"op": "divide", "args": [{"op": "fact", "fact_id": fid, "path": "total"}, {"op": "constant", "constant": self.factor}]},
                     operational_fact_ids=[fid], term_fact_ids=[], scopes=[])
        return AssessmentBundle(assessments=[a])


class ReportPersistenceTests(unittest.TestCase):
    """Reuse the real invoice mailbox setup, not the ingestion-stage test methods."""
    def setUp(self):
        fixtures.PersistenceTests.setUp(self)
        self.extractor = ReportingExtractor()
        extract_pending(self.state, extractor=self.extractor)
        self.auditor = FakeAuditor()

    def test_report_schema_citations_cache_and_outbox_idempotence(self) -> None:
        report = build_report(str(self.state), "2032-02-01", auditor=self.auditor)
        self.assertEqual(schema_errors(report), [])
        self.assertEqual(citation_errors(report, EXAMPLES / "overcharge/docs"), [])
        self.assertEqual(report["findings"][0]["amount_usd"], "2095.29")
        self.assertEqual(report["findings"][0]["action"], "draft:dispute")
        self.assertEqual(build_report(str(self.state), "2032-02-01", auditor=self.auditor), report)
        self.assertEqual(self.auditor.calls, 1)
        auto = build_report(str(self.state), "2032-02-01", "autonomous", auditor=self.auditor)
        self.assertEqual(auto["findings"][0]["action"], "human_review")
        self.assertEqual(self.auditor.calls, 1)
        build_report(str(self.state), "2032-02-02", "autonomous", auditor=self.auditor)
        with FactStore(self.state) as store:
            rows = list(store.connection.execute("SELECT status, payload_json FROM action_outbox"))
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(json.loads(row[1])["transport"] == "not_sent" for row in rows))

    def test_close_and_override_are_scoped_and_provenanced(self) -> None:
        build_report(str(self.state), "2032-02-01", auditor=self.auditor)
        data = feedback_record("override_amount", scope_kind="one_off", fact={
            "type": "dispute_closed", "subject": {"invoice_id": "TX-US-2032-5394"},
            "value": {"status": "settled", "amount_usd": "212.40"}, "evidence": "AP settlement"})
        data["items"][0]["finding_key"]["invoice_id"] = "TX-US-2032-5394"
        data["items"][0]["scope"]["applies_to"] = {"invoice_id": "TX-US-2032-5394"}
        path = self.root / "feedback.json"
        path.write_text(canonical_json(data))
        ingest_feedback(path, self.state)
        report = build_report(str(self.state), "2032-02-01", auditor=self.auditor)
        item = report["findings"][0]
        self.assertEqual(item["amount_usd"], "212.40")
        self.assertEqual(item["learned_at"], {"feedback_item_id": "fb-1", "batch": "batch_01"})
        self.assertEqual(len(item["supersedes"]), 1)
        data["items"][0].update(item_id="fb-2", decision="close")
        path.write_text(canonical_json(data))
        ingest_feedback(path, self.state)
        self.assertEqual(build_report(str(self.state), "2032-02-01", auditor=self.auditor)["findings"], [])
        with FactStore(self.state) as store:
            self.assertEqual(store.connection.execute("SELECT count(*) FROM action_outbox WHERE status IN ('ready', 'draft')").fetchone()[0], 0)

    def test_revision_id_changes_only_when_result_changes(self) -> None:
        old = build_report(str(self.state), "2032-02-01", auditor=self.auditor)["findings"][0]
        self.auditor.factor, self.auditor.cache_key = "2", "test-auditor:2"
        new = build_report(str(self.state), "2032-02-02", auditor=self.auditor)["findings"][0]
        self.assertEqual(new["amount_usd"], "1047.65")
        self.assertEqual(new["supersedes"], [old["id"]])
        self.assertNotEqual(new["id"], old["id"])

    def test_cached_mode_switch_reactivates_only_current_drafts(self) -> None:
        supervised = build_report(str(self.state), "2032-02-01", auditor=self.auditor)
        build_report(str(self.state), "2032-02-01", "autonomous", auditor=self.auditor)
        with FactStore(self.state) as store:
            self.assertEqual(store.connection.execute("SELECT count(*) FROM action_outbox WHERE status = 'draft'").fetchone()[0], 0)
        self.assertEqual(build_report(str(self.state), "2032-02-01", auditor=self.auditor), supervised)
        with FactStore(self.state) as store:
            self.assertEqual(store.connection.execute("SELECT count(*) FROM action_outbox WHERE status = 'draft'").fetchone()[0], 1)
            self.assertEqual(store.connection.execute("SELECT count(*) FROM action_outbox").fetchone()[0], 2)
        self.assertEqual(self.auditor.calls, 1)

    def test_historical_report_does_not_restore_old_outbox_action(self) -> None:
        old = build_report(str(self.state), "2032-02-01", auditor=self.auditor)
        build_report(str(self.state), "2032-02-02", "autonomous", auditor=self.auditor)
        self.assertEqual(build_report(str(self.state), "2032-02-01", auditor=self.auditor), old)
        with FactStore(self.state) as store:
            self.assertEqual(store.connection.execute("SELECT count(*) FROM action_outbox WHERE status = 'draft'").fetchone()[0], 0)

    def test_report_rejects_incomplete_extraction_instead_of_claiming_clean(self) -> None:
        self.extractor.cache_key = "failed-new-reader"
        self.extractor.extract = Mock(side_effect=ValueError("failure"))
        extract_pending(self.state, extractor=self.extractor)
        with self.assertRaisesRegex(ValueError, "incomplete"):
            build_report(str(self.state), "2032-02-01", auditor=self.auditor)

    def test_cli_report_stdout_is_only_json(self) -> None:
        self.auditor.close = Mock()
        output = io.StringIO()
        with patch("harness.reporting.ClaudeAuditor", return_value=self.auditor), redirect_stdout(output):
            self.assertEqual(main(["report", "--as-of", "2032-02-01", "--state-dir", str(self.state)]), 0)
        self.assertEqual(schema_errors(json.loads(output.getvalue())), [])


if __name__ == "__main__":
    unittest.main()

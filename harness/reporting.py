"""As-of reports, scoped feedback, revision lineage, and idempotent action outbox."""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Protocol

from grader.validate import schema_errors

from .actions import PreparedFinding, select_actions
from .audit_context import AuditContext
from .audit_models import AssessmentBundle, Expression
from .auditor import ClaudeAuditor
from .calculations import evaluate, money
from .claude_extractor import canonical_json, digest
from .fact_store import FactStore
from .facts import invoice_key
from .feedback import load_feedback, matches
from .providers import backend

REPORT_POLICY_VERSION = "report-policy:3"  # Bump when deterministic reporting or safety semantics change.


class Auditor(Protocol):
    cache_key: str

    def assess(self, context: AuditContext) -> AssessmentBundle: ...


def load_context(store: FactStore, as_of: date) -> AuditContext:
    documents = {d.source.doc_ref: d for d in store.documents(as_of=as_of.isoformat())}
    issues = {}
    for ref, document in documents.items():
        status = store.status(ref)
        if (document.extraction.status != "ready" or status is None or status["status"] != "ready"
                or status["reader_version"] != document.reader_version):
            raise ValueError(f"{ref}: ingestion/structured extraction incomplete; retry extraction before reporting")
        issues[ref] = status["issues"]
    records = {record["fact_id"]: record for record in store.fact_records(as_of=as_of.isoformat())}
    return AuditContext(as_of, records, documents, load_feedback(store, as_of), issues)


def context_hash(context: AuditContext, provider_version: str) -> str:
    return digest({
        "policy": REPORT_POLICY_VERSION, "as_of": context.as_of.isoformat(), "provider": provider_version, "records": context.records,
        "feedback": [f.model_dump(mode="json", by_alias=True) for f in context.feedback],
        "documents": {ref: doc.to_dict() for ref, doc in context.documents.items()}, "issues": context.issues,
    })


def referenced_facts(expression: Expression) -> set[str]:
    result = {expression.fact_id} if expression.op == "fact" else set()
    for arg in expression.args:
        result.update(referenced_facts(arg))
    return result


def prepare(bundle: AssessmentBundle, context: AuditContext) -> list[PreparedFinding]:
    prepared = []
    seen = set()
    for original in bundle.assessments:
        assessment = original.model_copy(deep=True)
        context.validate_assessment(assessment)
        key = assessment.key
        if key.identity() in seen:
            raise ValueError("duplicate finding key in assessment output")
        seen.add(key.identity())
        if key.finding_class == "expiry" and not 0 <= (assessment.deadline - context.as_of).days <= 60:
            continue
        if key.finding_class == "late_fee_risk":
            if not 1 <= (assessment.deadline - context.as_of).days <= 14:
                continue
            if any(f["kind"] == "payment" and invoice_key(f["invoice_number"]) == key.invoice_id
                   and f["status"].casefold() == "paid" and (f.get("paid_date") or {}).get("value")
                   and date.fromisoformat(f["paid_date"]["value"]) <= context.as_of for f in context.facts.values()):
                continue
        if key.finding_class == "cashback" and assessment.period_end >= context.as_of:
            continue
        expressions = ([assessment.amount] if assessment.amount else []) + assessment.conflict_amounts
        for expression in expressions:
            if not referenced_facts(expression) <= set(assessment.term_fact_ids + assessment.operational_fact_ids):
                raise ValueError("calculation uses undeclared supporting facts")
        amount = evaluate(assessment.amount, context.facts) if assessment.amount else None
        ranges = [max(Decimal(0), evaluate(e, context.facts)) for e in assessment.conflict_amounts] or None
        if amount is not None:
            amount = Decimal(money(max(Decimal(0), amount)))
        if ranges:
            ranges = sorted(Decimal(money(value)) for value in ranges)
        learned = None
        suppressed = False
        for feedback in context.feedback:
            if not matches(feedback, assessment, context.as_of):
                continue
            item = feedback.item
            same_key = item.finding_key.identity() == key.identity()
            if same_key and item.decision in ("close", "reject"):
                suppressed = True
            if same_key and item.decision == "override_amount":
                amount = Decimal(money(Decimal(str(item.fact.value["amount_usd"]))))
                assessment.verdict = "finding" if amount > 0 else "no_finding"
                ranges = None
                learned = {"feedback_item_id": item.item_id, "batch": feedback.after_batch}
            if item.item_id in assessment.feedback_ids:
                learned = {"feedback_item_id": item.item_id, "batch": feedback.after_batch}
        if suppressed:
            continue
        if assessment.verdict == "finding" and key.finding_class != "expiry" and (amount is None or amount <= 0):
            assessment.verdict = "no_finding"
        if assessment.verdict != "finding":
            amount = None
        chosen = assessment.term_fact_ids + assessment.operational_fact_ids
        assessment.confidence = min([assessment.confidence, *[context.facts[fid]["confidence"] for fid in chosen]])
        prepared.append(PreparedFinding(assessment, amount, ranges, context.safety_proof(assessment), learned))
    return prepared


def report_items(report: dict) -> list[dict]:
    return report["findings"] + [i for group in report["portfolio"].values() for i in group]


def _key(item: dict) -> tuple:
    if item["class"] == "cashback":
        return item["class"], item["contract_id"], item["period"], item["credit_type"]
    return item["class"], item.get("contract_id") if item["class"] == "expiry" else item.get("invoice_id")


def render(items: list[PreparedFinding], context: AuditContext, mode: str, previous: dict[tuple, dict]) -> dict:
    actions = select_actions(items, mode, context.as_of)
    report = {"schema_version": "1.0", "as_of": context.as_of.isoformat(), "mode": mode,
              "findings": [], "portfolio": {"cashback": [], "expiry": [], "late_fee_risk": []}}
    for prepared in sorted(items, key=lambda p: p.assessment.key.identity()):
        a = prepared.assessment
        item = {k: v for k, v in a.key.model_dump(mode="json", by_alias=True).items() if v is not None}
        item.update({
            "verdict": a.verdict, "amount_usd": money(prepared.amount) if prepared.amount is not None else None,
            "amount_range": [money(v) for v in prepared.amount_range] if a.verdict == "conflict" else None,
            "deadline": a.deadline.isoformat() if a.deadline else None, "action": actions[a.key.identity()],
            "confidence": a.confidence, "evidence": [e.model_dump(exclude={"layer"}) for e in a.evidence],
            "missing_doc": a.missing_doc.model_dump(exclude={"category"}, exclude_none=True) if a.verdict == "cannot_determine" else None,
            "supersedes": [], "learned_at": prepared.learned_at,
        })
        semantic = [a.key.identity(), a.verdict, item["amount_usd"], item["amount_range"], item["deadline"]]
        item["id"] = "finding:" + digest(semantic)[:24]
        old = previous.get(a.key.identity())
        if old is not None and old["id"] != item["id"]:
            item["supersedes"] = [old["id"]]
        elif old is not None:
            item["supersedes"] = old.get("supersedes", [])
            if old.get("retro"):
                item["retro"] = True
        if old is None and a.key.invoice_id:
            invoice_dates = [(f.get("invoice_date") or {}).get("value") for f in context.facts.values()
                             if f["kind"] == "invoice" and invoice_key(f["invoice_number"]) == a.key.invoice_id]
            term_docs = {context.records[fid]["doc_ref"] for fid in a.term_fact_ids}
            for fid, fact in context.facts.items():
                if context.records[fid]["doc_ref"] not in term_docs:
                    continue
                if (fact["kind"] == "amendment" and fact.get("signature_status") == "signed"
                        and fact.get("contract_id") == a.key.contract_id and (fact.get("effective_date") or {}).get("value")):
                    received = context.documents[context.records[fid]["doc_ref"]].source.received_at[:10]
                    if fact["effective_date"]["value"] < received and any(d and d < received for d in invoice_dates):
                        item["retro"] = True
        if a.key.finding_class in report["portfolio"]:
            report["portfolio"][a.key.finding_class].append(item)
        else:
            report["findings"].append(item)
    errors = schema_errors(report)
    if errors:
        raise ValueError("invalid report: " + "; ".join(errors[:5]))
    return report


def build_report(state_dir: str, as_of: str, mode: str = "supervised", *, auditor: Auditor | None = None) -> dict:
    day = date.fromisoformat(as_of)
    if mode not in ("supervised", "autonomous"):
        raise ValueError("unknown reporting mode")
    owns_auditor = auditor is None
    if auditor is None:
        if backend() == "codex":
            from .codex_client import CodexClient
            client = CodexClient()
            auditor = ClaudeAuditor(client=client, model=client.model)
        else:
            auditor = ClaudeAuditor()
    try:
        with FactStore(state_dir) as store:
            context = load_context(store, day)
            input_hash = context_hash(context, auditor.cache_key)
            report_id = digest(["report:1", input_hash, mode])
            existing = store.connection.execute("SELECT report_json FROM reports WHERE report_id = ?", (report_id,)).fetchone()
            cached = store.connection.execute("SELECT assessments_json FROM audit_runs WHERE input_hash = ?", (input_hash,)).fetchone()
            bundle = AssessmentBundle.model_validate_json(cached[0]) if cached else auditor.assess(context)
            # Revalidate injected providers too; never trust model_construct or assignment.
            bundle = AssessmentBundle.model_validate_json(bundle.model_dump_json(by_alias=True))
            prepared = prepare(bundle, context)
            with store.transaction():
                if context_hash(load_context(store, day), auditor.cache_key) != input_hash:
                    raise ValueError("state changed while the report was evaluated; retry")
                previous = {}
                for row in store.connection.execute("SELECT report_json FROM reports WHERE as_of <= ? ORDER BY as_of DESC, rowid DESC", (as_of,)):
                    for item in report_items(json.loads(row[0])):
                        previous.setdefault(_key(item), item)
                report = json.loads(existing[0]) if existing else render(prepared, context, mode, previous)
                store.connection.execute("INSERT OR IGNORE INTO audit_runs VALUES (?, ?, ?, ?)",
                                         (input_hash, as_of, auditor.cache_key, bundle.model_dump_json(by_alias=True)))
                store.connection.execute("INSERT OR IGNORE INTO reports VALUES (?, ?, ?, ?, ?)",
                                         (report_id, as_of, mode, input_hash, canonical_json(report)))
                latest = store.connection.execute("SELECT max(as_of) FROM reports").fetchone()[0]
                if as_of < latest:
                    # Historical queries must not replace today's pending actions.
                    return report
                by_key = {p.assessment.key.identity(): p for p in prepared}
                action_ids = {digest([item["id"], item["action"], by_key[_key(item)].proof.recipient]) for item in report_items(report)}
                for old in store.connection.execute(
                    """SELECT o.action_id FROM action_outbox o JOIN reports r ON r.report_id = o.report_id
                       WHERE o.status IN ('ready', 'draft') AND r.as_of <= ?""", (as_of,),
                ).fetchall():
                    if old["action_id"] not in action_ids:
                        store.connection.execute("UPDATE action_outbox SET status = 'superseded' WHERE action_id = ?", (old["action_id"],))
                for item in report_items(report):
                    p = by_key[_key(item)]
                    action = item["action"]
                    status = "draft" if action.startswith("draft:") else "held" if action == "human_review" else "ready" if action.startswith("execute:") else "recorded"
                    payload = {"finding": item, "rationale": p.assessment.rationale,
                               "safety_checks": vars(p.proof), "transport": "not_sent"}
                    store.connection.execute("""INSERT INTO action_outbox VALUES (?, ?, ?, ?, ?, ?, ?)
                                             ON CONFLICT(action_id) DO UPDATE SET report_id = excluded.report_id,
                                             payload_json = excluded.payload_json, status = excluded.status""",
                                             (digest([item["id"], action, p.proof.recipient]), report_id, item["id"], action,
                                              p.proof.recipient, canonical_json(payload), status))
                return report
    finally:
        if owns_auditor:
            auditor.close()

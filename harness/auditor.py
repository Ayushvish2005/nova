"""Claude clause interpretation behind an injectable, cached assessment interface."""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anthropic
from pydantic import ValidationError

from .audit_context import AuditContext
from .audit_models import AssessmentBundle
from .calculations import evaluate
from .claude_extractor import DEFAULT_MODEL, canonical_json, digest
from .facts import invoice_key

ROOT = Path(__file__).resolve().parents[1]
AUDIT_PROMPT = (Path(__file__).with_name("prompts") / "audit.txt").read_text() + "\n\nGLOSSARY:\n" + (ROOT / "GLOSSARY.md").read_text()
MAX_INVOICE_TARGETS = 4


def compact_claim(value):
    """Drop empty optional properties, never array entries or source values."""
    if isinstance(value, dict):
        return {key: compact_claim(item) for key, item in value.items()
                if item is not None and item != [] and item != {}}
    if isinstance(value, list):
        return [compact_claim(item) for item in value]
    return value


def evaluation_groups(context: AuditContext) -> list[dict]:
    facts = context.facts
    invoices = {invoice_key(f["invoice_number"]): f.get("contract_id") or "invoice:" + invoice_key(f["invoice_number"])
                for f in facts.values() if f["kind"] == "invoice" and f["document_type"] != "credit_note"}
    contracts = {f["contract_id"] for f in facts.values() if f["kind"] == "contract"}
    groups = {g: set() for g in contracts | set(invoices.values())}
    shipments: dict[str, set[str]] = {}
    for f in facts.values():
        if f["kind"] == "invoice_line" and f.get("shipment_id"):
            group = invoices.get(invoice_key(f["invoice_number"]))
            if group:
                shipments.setdefault(f["shipment_id"], set()).add(group)
    for fid, f in facts.items():
        targets = set()
        if f.get("contract_id") in groups:
            targets.add(f["contract_id"])
        if f.get("invoice_number"):
            group = invoices.get(invoice_key(f["invoice_number"]))
            if group:
                targets.add(group)
        for number in f.get("scope", {}).get("invoice_ids", []):
            group = invoices.get(invoice_key(number))
            if group:
                targets.add(group)
        for sid in [f.get("shipment_id"), *f.get("scope", {}).get("shipment_ids", [])]:
            targets.update(shipments.get(sid, set()))
        for ref in f.get("references", []):
            if ref["entity_type"] == "invoice":
                group = invoices.get(invoice_key(ref["identifier"]))
                if group:
                    targets.add(group)
            elif ref["entity_type"] == "contract" and ref["identifier"] in groups:
                targets.add(ref["identifier"])
            elif ref["entity_type"] == "shipment":
                targets.update(shipments.get(ref["identifier"], set()))
        for target in targets:
            groups[target].add(fid)
    # Unresolved standalone messages (for example a month-only waiver) must not
    # disappear just because they lack an invoice identifier. Their relationships
    # remain unproven; the evaluator must establish applicability from evidence.
    assigned = set().union(*groups.values()) if groups else set()
    unassigned = set(facts) - assigned
    result = []
    for group, identifiers in sorted(groups.items()):
        families = {context.records[fid]["doc_ref"].partition("#")[0] for fid in identifiers}
        identifiers = identifiers | unassigned
        families.update(context.records[fid]["doc_ref"].partition("#")[0] for fid in unassigned)
        invoice_targets = sorted(i for i, g in invoices.items() if g == group)
        scoped_feedback = [f.model_dump(mode="json", by_alias=True) for f in context.feedback
                           if f.item.scope.applies_to.get("contract_id") == group or f.item.finding_key.invoice_id in invoice_targets]
        payload = {
            "as_of": context.as_of.isoformat(), "invoice_targets": invoice_targets,
            "contract_targets": [group] if group in contracts else [],
            "records": [compact_claim(context.records[fid]) for fid in sorted(identifiers)], "feedback": scoped_feedback,
            "source_metadata": {ref: doc.source.metadata for ref, doc in context.documents.items()
                                if ref.partition("#")[0] in families},
        }
        if len(invoice_targets) <= MAX_INVOICE_TARGETS:
            result.append(payload)
        else:
            # Bound output size without losing older invoices, credits, tracking,
            # or competing instruments needed for duplicates and portfolio math.
            for start in range(0, len(invoice_targets), MAX_INVOICE_TARGETS):
                result.append({**payload, "invoice_targets": invoice_targets[start:start + MAX_INVOICE_TARGETS],
                               "contract_targets": []})
            if payload["contract_targets"]:
                result.append({**payload, "invoice_targets": []})
    return result


class ClaudeAuditor:
    def __init__(self, *, client=None, model: str | None = None) -> None:
        self.model = model or os.environ.get("ANTHROPIC_MODEL") or DEFAULT_MODEL
        self._owns_client = client is None
        self.client = client
        self.cache_key = "audit:2:" + digest({"model": self.model, "prompt": AUDIT_PROMPT,
                                               "invoice_target_limit": MAX_INVOICE_TARGETS,
                                               "schema": AssessmentBundle.model_json_schema(),
                                               "base_url": str(getattr(client, "base_url", os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")))})

    def close(self) -> None:
        if self._owns_client and self.client is not None:
            self.client.close()

    def assess(self, context: AuditContext) -> AssessmentBundle:
        groups = evaluation_groups(context)
        if not groups:
            return AssessmentBundle(assessments=[])
        if self.client is None:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise ValueError("ANTHROPIC_API_KEY is required to evaluate uncached reports")
            self.client = anthropic.Anthropic(timeout=90.0, max_retries=1)

        def run(group: dict) -> list:
            content = canonical_json(group)
            if len(content) > 350000:
                raise ValueError("audit group exceeds context limit; split its extraction/context before reporting")
            messages = [{"role": "user", "content": content}]
            for attempt in range(2):
                try:
                    response = self.client.messages.create(
                        model=self.model, max_tokens=16000, system=AUDIT_PROMPT, messages=messages,
                        tools=[{"name": "propose_assessments", "description": "Return source-backed audit proposals, never actions.",
                                "input_schema": AssessmentBundle.model_json_schema(mode="serialization")}], tool_choice={"type": "auto"},
                    )
                except anthropic.APIError as exc:
                    raise ValueError(f"audit API failed: {type(exc).__name__}, status={getattr(exc, 'status_code', None)}") from exc
                if response.stop_reason not in ("end_turn", "tool_use"):
                    raise ValueError(f"audit response incomplete: {response.stop_reason}")
                calls = [b for b in response.content if b.type == "tool_use"]
                try:
                    if len(calls) != 1 or calls[0].name != "propose_assessments" or response.stop_reason != "tool_use":
                        raise ValueError("return one propose_assessments call")
                    bundle = AssessmentBundle.model_validate_json(canonical_json(calls[0].input))
                    seen = set()
                    for a in bundle.assessments:
                        if a.key.identity() in seen:
                            raise ValueError("duplicate finding key")
                        seen.add(a.key.identity())
                        if a.key.invoice_id and a.key.invoice_id not in group["invoice_targets"]:
                            raise ValueError("assessment is outside the requested invoice group")
                        if not a.key.invoice_id and a.key.contract_id not in group["contract_targets"]:
                            raise ValueError("assessment is outside the requested contract group")
                        context.validate_assessment(a)
                        for expression in ([a.amount] if a.amount else []) + a.conflict_amounts:
                            evaluate(expression, context.facts)
                    covered = {a.key.invoice_id for a in bundle.assessments}
                    if not set(group["invoice_targets"]) <= covered:
                        raise ValueError("evaluate every invoice target, including clean invoices")
                    return bundle.assessments
                except (ValueError, ValidationError) as exc:
                    detail = "schema validation failed" if isinstance(exc, ValidationError) else str(exc)
                    if attempt:
                        raise ValueError(f"audit proposal rejected: {detail}") from exc
                    messages.append({"role": "assistant", "content": [b.model_dump(mode="json") for b in response.content]})
                    correction = "Correct the proposal using held facts only: " + detail
                    messages.append({"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": c.id, "is_error": True, "content": correction} for c in calls
                    ] if calls else correction})
            raise ValueError("audit proposal validation exhausted")

        with ThreadPoolExecutor(max_workers=4) as pool:
            batches = list(pool.map(run, groups))
        return AssessmentBundle(assessments=[a for batch in batches for a in batch])

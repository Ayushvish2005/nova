"""Check that structured evidence actually resolves in the ingested document."""
from __future__ import annotations

from .facts import Evidence, FactBundle
from .models import Document


class EvidenceError(ValueError):
    pass


def validate_evidence(bundle: FactBundle, document: Document, known_refs: set[str]) -> None:
    pages = {page.number: page for page in document.extraction.pages}

    def check(evidence: Evidence) -> None:
        if evidence.doc != document.source.doc_ref:
            raise EvidenceError("evidence must cite the document being extracted")
        page = pages.get(evidence.page)
        if page is None:
            raise EvidenceError(f"evidence page {evidence.page} does not exist")
        if evidence.layer == "native":
            text = page.native_text
        elif evidence.layer == page.method:
            text = page.text
        else:
            raise EvidenceError(f"evidence layer {evidence.layer} unavailable on page {evidence.page}")
        if " ".join(evidence.quote.split()) not in " ".join(text.split()):
            raise EvidenceError(f"evidence quote not found on page {evidence.page} in {evidence.layer} text")

    for record in [*bundle.facts, *bundle.issues]:
        for evidence in record.evidence:
            check(evidence)
    for fact in bundle.facts:
        for reference in fact.references:
            if reference.doc_ref is not None and reference.doc_ref not in known_refs:
                raise EvidenceError("relationship invents an unknown document reference")

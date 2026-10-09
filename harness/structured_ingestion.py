"""Run independent documents concurrently; commit validated fact sets on one writer."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .claude_extractor import ClaudeExtractor, ExtractionInput, StructuredExtractionError, StructuredResult
from .fact_store import FactStore
from .providers import extraction_provider


class Extractor(Protocol):
    cache_key: str
    model: str

    def extract(self, item: ExtractionInput) -> StructuredResult: ...


@dataclass
class FactSummary:
    extracted: int = 0
    reused: int = 0
    failed: int = 0
    issues: int = 0
    problems: list[str] = field(default_factory=list)


def extract_pending(
    state_dir: str | Path, *, batch: str | None = None, extractor: Extractor | None = None, workers: int = 4,
) -> FactSummary:
    if workers < 1 or workers > 16:
        raise ValueError("workers must be between 1 and 16")
    owns_extractor = extractor is None
    extractor = extractor if extractor is not None else extraction_provider()
    summary = FactSummary()
    try:
        with FactStore(state_dir) as store:
            documents = list(store.documents())
            if batch is not None and not any(d.source.batch == batch for d in documents):
                raise ValueError(f"no ingested documents for batch {batch}")
            families: dict[str, list[dict[str, str | None]]] = {}
            for doc in documents:
                key = doc.source.parent_ref or doc.source.doc_ref
                families.setdefault(key, []).append({
                    "doc_ref": doc.source.doc_ref, "kind": doc.source.kind, "filename": doc.source.filename,
                })
            pending = []
            for doc in documents:
                if batch is not None and doc.source.batch != batch:
                    continue
                if doc.extraction.status != "ready":
                    summary.failed += 1
                    summary.problems.append(f"{doc.source.doc_ref}: document text is {doc.extraction.status}")
                    continue
                item = ExtractionInput(doc, families[doc.source.parent_ref or doc.source.doc_ref])
                if store.cached(item, extractor.cache_key):
                    with store.transaction():
                        store.activate(doc.source.doc_ref, store.extraction_id(item, extractor.cache_key))
                    summary.reused += 1
                else:
                    pending.append(item)
            # No SQLite connection or transaction crosses a thread/API boundary.
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(extractor.extract, item): item for item in pending}
                for future in as_completed(futures):
                    item = futures[future]
                    try:
                        result = future.result()
                        with store.transaction():
                            store.save_result(item, extractor.cache_key, extractor.model, result=result)
                        summary.extracted += 1
                        summary.issues += len(result.bundle.issues)
                    except Exception as exc:
                        # Unexpected backend failures are visible but cannot leak endpoint secrets.
                        error = str(exc) if isinstance(exc, StructuredExtractionError) else f"extraction failed: {type(exc).__name__}"
                        with store.transaction():
                            store.save_result(item, extractor.cache_key, extractor.model, error=error,
                                              responses=getattr(exc, "responses", []))
                        summary.failed += 1
                        summary.problems.append(f"{item.document.source.doc_ref}: {error}")
    finally:
        if owns_extractor:
            extractor.close()
    return summary

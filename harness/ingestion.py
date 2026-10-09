"""Ingest batches without classifying charges or making audit decisions."""
from __future__ import annotations

from pathlib import Path

from .models import Document, Extraction, IngestSummary
from .readers import DocumentReader, message_sources
from .store import StateStore


def ingest_batch(
    batch_dir: str | Path, state_dir: str | Path, *, reader: DocumentReader | None = None,
) -> IngestSummary:
    batch_dir = Path(batch_dir).resolve()
    if not batch_dir.is_dir():
        raise ValueError(f"batch directory does not exist: {batch_dir}")
    paths = sorted(p for p in batch_dir.rglob("*") if p.is_file() and p.suffix.lower() == ".eml")
    if not paths:
        raise ValueError(f"batch directory has no .eml files: {batch_dir}")
    reader = reader if reader is not None else DocumentReader()
    reader_version = reader.cache_key
    summary = IngestSummary()
    # Avoid repeatedly retrying the same failed attachment within a single batch.
    attempted: set[tuple[str, str]] = set()
    with StateStore(state_dir) as store, store.transaction():
        for path in paths:
            for source, data in message_sources(batch_dir, path):
                store.check_source(source)
                cached = store.cached_extraction(source.content_hash, source.format, reader_version)
                key = (source.content_hash, source.format)
                run = cached is None or (cached.status == "failed" and key not in attempted)
                if run:
                    attempted.add(key)
                    try:
                        extraction = reader.extract(data, source.format)
                        Document(source, reader_version, extraction).validate()
                    except Exception as exc:
                        # Persist the bytes and visible failure; another ingest can retry failed extraction.
                        extraction = Extraction("failed", error=f"{type(exc).__name__}: {exc}")
                    summary.extractions_run += 1
                else:
                    extraction = cached
                    summary.extractions_reused += 1
                document = Document(source, reader_version, extraction)
                if store.save(document, data, extracted=run):
                    summary.documents_added += 1
                else:
                    summary.documents_existing += 1
                if extraction.status != "ready":
                    summary.problems.append(f"{source.doc_ref}: {extraction.error}")
    return summary

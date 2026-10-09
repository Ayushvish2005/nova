"""Carrier-independent document records shared by readers and persistence."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from jsonschema import Draft202012Validator, FormatChecker


@dataclass(frozen=True)
class Source:
    doc_ref: str
    batch: str
    kind: Literal["email", "attachment"]
    content_hash: str
    format: str
    media_type: str
    received_at: str
    metadata: dict[str, Any]
    parent_ref: str | None = None
    filename: str | None = None
    attachment_index: int | None = None


@dataclass(frozen=True)
class OCRRegion:
    """Original recognized text and quadrilateral in rendered-image pixels."""

    text: str
    confidence: float
    box: list[list[float]]


@dataclass(frozen=True)
class Page:
    number: int
    text: str
    native_text: str
    method: Literal["grader", "ocr", "html"] = "grader"
    regions: list[OCRRegion] = field(default_factory=list)
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True)
class Extraction:
    status: Literal["ready", "failed", "unsupported"]
    pages: list[Page] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True)
class Document:
    source: Source
    reader_version: str
    extraction: Extraction
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def validate(self) -> None:
        """Reject malformed persisted records, including non-finite OCR numbers."""
        data = json.loads(json.dumps(self.to_dict(), allow_nan=False))
        document_validator().validate(data)
        numbers = [p.number for p in self.extraction.pages]
        if numbers != list(range(1, len(numbers) + 1)):
            raise ValueError("document pages must be contiguous and one-based")
        source = self.source
        if source.kind == "email":
            if any(value is not None for value in (source.parent_ref, source.filename, source.attachment_index)):
                raise ValueError("an email cannot have attachment fields")
            if not source.doc_ref.startswith(source.batch + "/") or "#" in source.doc_ref:
                raise ValueError("email reference must be relative to its batch")
        else:
            if not source.parent_ref.startswith(source.batch + "/") or "#" in source.parent_ref:
                raise ValueError("attachment parent must be an email in the same batch")
            if source.doc_ref != f"{source.parent_ref}#{source.filename}":
                raise ValueError("attachment reference must retain its email and exact filename")


@lru_cache(maxsize=1)
def document_validator() -> Draft202012Validator:
    path = Path(__file__).with_name("schema") / "document.schema.json"
    schema = json.loads(path.read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


@dataclass
class IngestSummary:
    documents_added: int = 0
    documents_existing: int = 0
    extractions_run: int = 0
    extractions_reused: int = 0
    problems: list[str] = field(default_factory=list)

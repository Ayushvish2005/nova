"""Bounded Claude JSON extraction with local schema and provenance validation."""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from typing import Any

import anthropic
from pydantic import ValidationError

from .fact_validation import EvidenceError, validate_evidence
from .facts import FactBundle
from .models import Document

DEFAULT_MODEL = "claude-sonnet-4-6"
EXTRACTION_TOOL = "record_document_facts"
SYSTEM_PROMPT = (Path(__file__).with_name("prompts") / "extract.txt").read_text()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return sha256(canonical_json(value).encode()).hexdigest()


@dataclass(frozen=True)
class ExtractionInput:
    document: Document
    known_sources: list[dict[str, str | None]]

    @property
    def input_hash(self) -> str:
        return digest({"document": self.document.to_dict(), "known_sources": self.known_sources})

    @property
    def known_refs(self) -> set[str]:
        return {str(source["doc_ref"]) for source in self.known_sources}


@dataclass
class StructuredResult:
    bundle: FactBundle
    responses: list[dict[str, Any]] = field(default_factory=list)


class StructuredExtractionError(ValueError):
    def __init__(self, message: str, responses: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.responses = responses or []


def _segments(document: Document, limit: int) -> list[dict[str, Any]]:
    """Split large pages without dropping text; retain original page numbers/layers."""
    segments = []
    for page in document.extraction.pages:
        # Both layers are supplied when OCR supplements an existing native text layer.
        layers = [("native", page.native_text)]
        if page.method in ("ocr", "html"):
            layers.append((page.method, page.text))
        for layer, text in layers:
            if not text.strip():
                continue
            start = 0
            while start < len(text):
                end = min(start + limit, len(text))
                if end < len(text):
                    newline = text.rfind("\n", start + limit // 2, end)
                    if newline > start:
                        end = newline + 1
                segments.append({"page": page.number, "layer": layer, "start": start, "text": text[start:end]})
                start = end
    return segments


class ClaudeExtractor:
    def __init__(
        self, *, client: Any = None, model: str | None = None, max_tokens: int = 12000,
        chunk_chars: int = 10000, chunk_pages: int = 2, repair_attempts: int = 1,
        chunk_workers: int = 1,
    ) -> None:
        if max_tokens < 1 or chunk_chars < 100 or chunk_pages < 1 or repair_attempts not in (0, 1, 2):
            raise ValueError("invalid extraction limits")
        if not 1 <= chunk_workers <= 4:
            raise ValueError("chunk_workers must be between 1 and 4")
        self.model = model or os.environ.get("ANTHROPIC_MODEL") or DEFAULT_MODEL
        self.max_tokens = max_tokens
        self.chunk_chars = chunk_chars
        self.chunk_pages = chunk_pages
        self.repair_attempts = repair_attempts
        self.chunk_workers = chunk_workers
        # A non-strict data-return tool avoids the API's global limits on optional
        # and union fields. The full Pydantic schema is enforced locally instead.
        self.schema = FactBundle.model_json_schema(mode="serialization")
        self.cache_key = "claude-facts:1:" + digest({
            "model": self.model, "base_url": str(getattr(client, "base_url", os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"))),
            "schema": FactBundle.model_json_schema(), "prompt": SYSTEM_PROMPT,
            "max_tokens": max_tokens, "chunk_chars": chunk_chars, "chunk_pages": chunk_pages,
            "repairs": repair_attempts, "anthropic": version("anthropic"), "pydantic": version("pydantic"),
        })
        self._owns_client = client is None
        if client is None:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise ValueError("ANTHROPIC_API_KEY is required for structured extraction; use --documents-only for ingestion alone")
            # The SDK reads ANTHROPIC_API_KEY and ANTHROPIC_BASE_URL; never log either.
            client = anthropic.Anthropic(timeout=90.0, max_retries=1)
        self.client = client

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def chunks(self, item: ExtractionInput) -> list[dict[str, Any]]:
        segments = _segments(item.document, self.chunk_chars)
        groups: list[list[dict[str, Any]]] = []
        for segment in segments:
            if not groups or (
                sum(len(s["text"]) for s in groups[-1]) + len(segment["text"]) > self.chunk_chars
                or len({s["page"] for s in groups[-1]} | {segment["page"]}) > self.chunk_pages
            ):
                groups.append([])
            groups[-1].append(segment)
        context = []
        if segments:
            # An explicitly marked, bounded heading excerpt for continuation tables.
            context = [{**segments[0], "text": segments[0]["text"][:2000]}]
        source = item.document.source
        return [{
            "doc_ref": source.doc_ref, "received_at": source.received_at,
            "parent_ref": source.parent_ref, "metadata": source.metadata,
            "known_sources": item.known_sources, "primary": group,
            "context": context if index else [], "chunk": index + 1, "chunks": len(groups),
        } for index, group in enumerate(groups)]

    def extract(self, item: ExtractionInput) -> StructuredResult:
        if item.document.extraction.status != "ready":
            raise StructuredExtractionError("document text extraction is not ready")
        chunks = self.chunks(item)
        if not chunks:
            return StructuredResult(FactBundle.model_validate_json(canonical_json({
                "facts": [], "issues": [{"code": "empty_document", "description": "No readable page text"}],
            })))
        facts, issues, responses = [], [], []
        if self.chunk_workers == 1:
            results = map(lambda chunk: self._extract_chunk(item, chunk), chunks)
            for bundle, attempts in results:
                facts.extend(bundle.facts)
                issues.extend(bundle.issues)
                responses.extend(attempts)
        else:
            # Input/output order remains deterministic; no SQLite writes occur here.
            with ThreadPoolExecutor(max_workers=self.chunk_workers) as pool:
                results = pool.map(lambda chunk: self._extract_chunk(item, chunk), chunks)
                for bundle, attempts in results:
                    facts.extend(bundle.facts)
                    issues.extend(bundle.issues)
                    responses.extend(attempts)
        # Repeated table context must not create identical records within a document.
        unique_facts = {canonical_json(f.model_dump(mode="json")): f for f in facts}
        unique_issues = {canonical_json(i.model_dump(mode="json")): i for i in issues}
        bundle = FactBundle(facts=list(unique_facts.values()), issues=list(unique_issues.values()))
        validate_evidence(bundle, item.document, item.known_refs)
        return StructuredResult(bundle, responses)

    def _extract_chunk(self, item: ExtractionInput, chunk: dict) -> tuple[FactBundle, list]:
        responses = []
        messages = [{"role": "user", "content": canonical_json(chunk)}]
        for attempt in range(self.repair_attempts + 1):
            try:
                response = self.client.messages.create(
                    model=self.model, max_tokens=self.max_tokens, system=SYSTEM_PROMPT,
                    messages=messages, tools=[{
                        "name": EXTRACTION_TOOL,
                        "description": "Return the complete structured extraction. This records data only and performs no business actions.",
                        "input_schema": self.schema,
                    }], tool_choice={"type": "auto"},
                )
            except anthropic.APIError as exc:
                # Exception messages may include request bodies or endpoint credentials.
                code = getattr(exc, "status_code", None)
                raise StructuredExtractionError(f"Claude API error: {type(exc).__name__}, status={code}", responses) from exc
            blocks = [block.model_dump(mode="json") for block in response.content]
            calls = [block for block in response.content if block.type == "tool_use"]
            text = canonical_json(calls[0].input) if len(calls) == 1 else ""
            responses.append({
                "id": response.id, "model": response.model, "stop_reason": response.stop_reason,
                "input_tokens": getattr(response.usage, "input_tokens", 0),
                "output_tokens": getattr(response.usage, "output_tokens", 0), "content": blocks,
            })
            if response.stop_reason not in ("end_turn", "tool_use"):
                raise StructuredExtractionError(f"Claude did not complete extraction: {response.stop_reason}", responses)
            try:
                if response.stop_reason != "tool_use" or len(calls) != 1 or calls[0].name != EXTRACTION_TOOL:
                    raise EvidenceError(f"return exactly one {EXTRACTION_TOOL} tool call")
                bundle = FactBundle.model_validate_json(text)
                validate_evidence(bundle, item.document, item.known_refs)
            except (ValidationError, EvidenceError) as exc:
                if isinstance(exc, ValidationError):
                    detail = canonical_json([{"loc": e["loc"], "message": e["msg"]}
                                             for e in exc.errors(include_input=False, include_context=False)])[:3000]
                else:
                    detail = str(exc)
                if attempt == self.repair_attempts:
                    raise StructuredExtractionError(f"Claude output failed validation: {detail}", responses) from exc
                correction = "Correct the extraction using only supplied evidence. Validation errors: " + detail
                messages.append({"role": "assistant", "content": blocks})
                messages.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": call.id, "is_error": True, "content": correction}
                    for call in calls
                ] if calls else correction})
                continue
            return bundle, responses
        raise StructuredExtractionError("extraction validation exhausted", responses)

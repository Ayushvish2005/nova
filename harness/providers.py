"""Explicit transport selection; never silently fall back between providers."""
from __future__ import annotations

import os
from typing import Any

from .claude_extractor import ClaudeExtractor, ExtractionInput, StructuredResult


def backend() -> str:
    value = os.environ.get("HARNESS_BACKEND", "claude")
    if value not in ("agy", "claude", "codex", "router"):
        raise ValueError("HARNESS_BACKEND must be agy, claude, codex, or router")
    return value


def _model_extraction_provider(*, model: str | None = None):
    selected = backend()
    if selected == "agy":
        from .agy_client import AgyClient
        client = AgyClient(model=model)
        return ClaudeExtractor(client=client, model=client.model or "agy-default", chunk_chars=1800,
                               chunk_pages=1, chunk_workers=3)
    if selected == "claude":
        return ClaudeExtractor(model=model)
    if selected == "router":
        from .router_client import RouterClient
        client = RouterClient()
        return ClaudeExtractor(client=client, model=client.model, chunk_chars=1800,
                               chunk_pages=1, chunk_workers=3)
    from .codex_client import CodexClient
    client = CodexClient(model=model)
    # Dense clauses and shipment tables expand substantially into strict JSON.
    # Bound text as well as page count; splitting retains every original offset.
    return ClaudeExtractor(client=client, model=client.model, chunk_chars=1800,
                           chunk_pages=1, chunk_workers=3)


class ExtractionRouter:
    """Use a complete local parse when possible; lazily create the LLM fallback."""

    def __init__(self, model: str | None = None) -> None:
        from .deterministic_extractor import LocalExtractor
        self.local = LocalExtractor()
        self.requested_model = model
        self.remote = None

    def for_item(self, item: ExtractionInput) -> ClaudeExtractor | _PreparedLocalResult:
        result = self.local.try_extract(item)
        if result is not None:
            return _PreparedLocalResult(self.local, result)
        if self.remote is None:
            self.remote = _model_extraction_provider(model=self.requested_model)
        return self.remote

    def close(self) -> None:
        if self.remote is not None:
            self.remote.close()


class _PreparedLocalResult:
    def __init__(self, reader: Any, result: StructuredResult) -> None:
        self.cache_key = reader.cache_key
        self.model = reader.model
        self.result = result

    def extract(self, item: ExtractionInput) -> StructuredResult:
        return self.result


def extraction_provider(*, model: str | None = None) -> ExtractionRouter:
    return ExtractionRouter(model)

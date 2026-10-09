"""Explicit transport selection; never silently fall back between providers."""
from __future__ import annotations

import os

from .claude_extractor import ClaudeExtractor


def backend() -> str:
    value = os.environ.get("HARNESS_BACKEND", "claude")
    if value not in ("claude", "codex"):
        raise ValueError("HARNESS_BACKEND must be claude or codex")
    return value


def extraction_provider(*, model: str | None = None):
    if backend() == "claude":
        return ClaudeExtractor(model=model)
    from .codex_client import CodexClient
    client = CodexClient(model=model)
    # Dense clauses and shipment tables expand substantially into strict JSON.
    # Bound text as well as page count; splitting retains every original offset.
    return ClaudeExtractor(client=client, model=client.model, chunk_chars=1800,
                           chunk_pages=1, chunk_workers=3)

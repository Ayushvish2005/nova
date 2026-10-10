"""Antigravity CLI (agy) transport adapter for structured extraction and audit proposals."""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

import anthropic
from jsonschema import Draft202012Validator, ValidationError as SchemaError

from .claude_extractor import StructuredExtractionError, canonical_json, digest


def clean_schema(schema: dict) -> dict:
    """Normalize schema for broad validator compatibility."""
    result = copy.deepcopy(schema)

    def visit(node):
        if isinstance(node, dict):
            for key in ("default", "format", "discriminator"):
                node.pop(key, None)
            if "oneOf" in node:
                node["anyOf"] = node.pop("oneOf")
            if "(?" in node.get("pattern", ""):
                node.pop("pattern")
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(result)
    return result


def extract_json_payload(text: str) -> dict | list:
    cleaned = text.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # Try finding markdown code block ```json ... ```
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            try:
                return json.JSONDecoder().raw_decode(match.group(1).lstrip())[0]
            except json.JSONDecodeError:
                pass
    # Try finding outer object braces { ... }
    start = cleaned.find("{")
    if start != -1:
        try:
            # AGY may append a second status object after the requested value.
            return json.JSONDecoder().raw_decode(cleaned[start:])[0]
        except json.JSONDecodeError:
            pass
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError:
            pass
    raise ValueError(f"Could not extract valid JSON from response: {cleaned[:300]}")


def sanitize_payload(obj: Any) -> Any:
    """Normalize common LLM schema edge cases (null for defaulted objects/arrays)."""
    if isinstance(obj, dict):
        res = {}
        for k, v in obj.items():
            if k == "quote" and isinstance(v, str):
                # AGY sometimes double-escapes JSON whitespace. Store the
                # source form; the normal evidence validator still requires an
                # exact whitespace-normalized match on the cited page.
                res[k] = v.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")
            elif k == "scope" and v is None:
                res[k] = {}
            elif v is None and k in ("references", "values", "conditions", "parties", "signatories",
                                     "supersedes_ids", "original_invoice_ids", "invoice_ids",
                                     "shipment_ids", "lanes", "charge_labels", "details",
                                     "facts", "issues", "assessments", "term_fact_ids", "operational_fact_ids"):
                res[k] = []
            else:
                res[k] = sanitize_payload(v)
        return res
    elif isinstance(obj, list):
        return [sanitize_payload(item) for item in obj]
    return obj


def repair_evidence_quotes(payload: Any, messages: list) -> Any:
    """Keep only exact source text when AGY combines a table header and later row."""
    sources: dict[tuple[Any, Any, Any], str] = {}
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            continue
        try:
            chunk = json.loads(content)
        except json.JSONDecodeError:
            continue
        if not isinstance(chunk, dict):
            continue
        grouped: dict[tuple[Any, Any, Any], list[tuple[int, str]]] = {}
        for segment in chunk.get("primary", []):
            key = (chunk.get("doc_ref"), segment.get("page"), segment.get("layer"))
            grouped.setdefault(key, []).append((int(segment.get("start", 0)), segment.get("text", "")))
        for key, parts in grouped.items():
            sources[key] = "".join(text for _, text in sorted(parts))

    def normalized(value: str) -> str:
        return " ".join(value.split())

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            quote = value.get("quote")
            key = (value.get("doc"), value.get("page"), value.get("layer"))
            source = sources.get(key)
            if isinstance(quote, str) and source and normalized(quote) not in normalized(source):
                candidates = [line.strip() for line in quote.splitlines() if line.strip()
                              and normalized(line) in normalized(source)]
                if candidates:
                    value["quote"] = max(candidates, key=lambda line: len(normalized(line)))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return payload


import threading

_AGY_SLOTS = threading.BoundedSemaphore(8)


class AgyClient:
    """Invokes the agy CLI non-interactively in print mode."""

    def __init__(self, *, model: str | None = None, effort: str | None = None, timeout: float = 300.0):
        self.model = model or os.environ.get("AGY_MODEL")
        self.effort = effort or os.environ.get("AGY_EFFORT", "low")
        self.timeout = float(os.environ.get("AGY_TIMEOUT", str(timeout)))
        if not shutil.which("agy"):
            raise ValueError("agy CLI executable not found in PATH")
        cache_dir_env = os.environ.get("AGY_CACHE_DIR")
        if cache_dir_env:
            self.cache_dir = Path(cache_dir_env).resolve()
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        else:
            default_cache = Path(__file__).resolve().parents[1] / "out" / "model_cache"
            default_cache.mkdir(parents=True, exist_ok=True)
            self.cache_dir = default_cache

        self.base_url = "agy-cli:" + digest([self.model or "default", self.effort or "default", "print-json:1"])
        self.messages = self

    def close(self) -> None:
        pass

    def create(self, *, system: str, messages: list, tools: list, **kwargs) -> anthropic.types.Message:
        if len(tools) != 1:
            raise ValueError("Agy transport expects exactly one data-return tool schema")
        tool = tools[0]
        schema = clean_schema(tool["input_schema"])
        validator = Draft202012Validator(schema)

        prompt = (
            "You are an automated structured data processor. Do NOT use tools, execute commands, or inspect files.\n"
            f"Your sole task is to return a valid JSON object matching the input schema for tool '{tool['name']}'.\n"
            "Output ONLY the raw JSON object. Do not wrap in conversational text or provide explanations.\n\n"
            f"INPUT SCHEMA FOR {tool['name']}:\n"
            f"{canonical_json(schema)}\n\n"
            "INSTRUCTIONS AND RULES:\n"
            f"{system}\n\n"
            "INPUT DATA:\n"
            f"{canonical_json(messages)}\n\n"
            f"Return ONLY the valid JSON object for {tool['name']}:"
        )

        cache_key = digest([self.base_url, tool["name"], schema, prompt])
        cache_path = self.cache_dir / f"{cache_key}.json" if self.cache_dir else None

        if cache_path and cache_path.is_file():
            try:
                cached = anthropic.types.Message.model_validate_json(cache_path.read_text())
                if len(cached.content) == 1 and cached.content[0].type == "tool_use" and cached.content[0].name == tool["name"]:
                    cached.content[0].input = repair_evidence_quotes(
                        sanitize_payload(cached.content[0].input), messages)
                    validator.validate(cached.content[0].input)
                    return cached
            except (ValueError, OSError, SchemaError):
                pass

        request_instruction = (
            "Read request.txt from the current directory, follow it exactly, and return the required JSON. "
            "Use only the file-reading capability needed for request.txt; do not inspect other files or use other tools."
        )
        cmd = [
            "agy", "-p", request_instruction,
            "--sandbox",
            "--disable-slash-commands",
            "--output-format", "json",
            "--json-schema", canonical_json(schema),
            "--print-timeout", f"{max(1, int(self.timeout))}s",
        ]
        if self.effort:
            cmd.extend(["--effort", self.effort])
        if self.model:
            cmd.extend(["--model", self.model])

        proc = None
        for attempt in range(2):
            try:
                # Source documents are untrusted. Keep the one permitted input
                # file outside the repository and run AGY's terminal sandbox.
                with tempfile.TemporaryDirectory(prefix="nova-agy-") as folder, _AGY_SLOTS:
                    Path(folder, "request.txt").write_text(prompt, encoding="utf-8")
                    proc = subprocess.run(cmd, capture_output=True, text=True,
                                          timeout=self.timeout + 5, cwd=folder)
            except subprocess.TimeoutExpired as exc:
                if attempt == 0:
                    continue
                raise StructuredExtractionError("agy CLI call timed out") from exc

            if proc.returncode != 0:
                err_msg = (proc.stderr or proc.stdout).strip()[:500]
                if attempt == 0 and ("stream reading error" in err_msg or "timed out" in err_msg or "network issue" in err_msg or "connection reset" in err_msg):
                    continue
                raise StructuredExtractionError(f"agy CLI exited with code {proc.returncode}: {err_msg}")
            break

        try:
            cli_output = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise StructuredExtractionError(f"agy CLI did not output valid JSON envelope: {proc.stdout[:300]}") from exc

        status = cli_output.get("status")
        if status not in ("SUCCESS", "COMPLETED", None):
            raise StructuredExtractionError(f"agy CLI run failed with status: {status}")

        # Native schema output is already isolated from commentary that may be
        # present in the CLI's human-readable response field.
        raw_response = cli_output.get("structured_output")
        if raw_response is None:
            raw_response = cli_output.get("response")
        if raw_response is None:
            raise StructuredExtractionError("agy CLI output contains no response or structured_output")

        if isinstance(raw_response, dict):
            payload = raw_response
        else:
            try:
                payload = extract_json_payload(str(raw_response))
            except Exception as exc:
                raise StructuredExtractionError(f"Failed to parse JSON from agy response: {exc}") from exc

        payload = repair_evidence_quotes(sanitize_payload(payload), messages)

        try:
            validator.validate(payload)
        except SchemaError as exc:
            raise StructuredExtractionError(f"agy payload failed schema validation: {exc.message}") from exc

        usage = cli_output.get("usage") or {}
        msg_id = str(cli_output.get("conversation_id") or uuid.uuid4())
        result = anthropic.types.Message.model_validate({
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "model": self.model or "agy-default",
            "content": [{"type": "tool_use", "id": "data_result", "name": tool["name"], "input": payload}],
            "stop_reason": "tool_use",
            "stop_sequence": None,
            "usage": {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
            },
        })

        if cache_path:
            try:
                with tempfile.NamedTemporaryFile(mode="w", dir=self.cache_dir, prefix="pending-", delete=False) as f:
                    f.write(result.model_dump_json())
                Path(f.name).replace(cache_path)
            except OSError:
                pass

        return result

"""OpenAI-compatible Groq/OpenRouter/Gemini transport with explicit failover."""
from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import anthropic
from jsonschema import Draft202012Validator, ValidationError as SchemaError

from .agy_client import clean_schema, sanitize_payload
from .claude_extractor import StructuredExtractionError, canonical_json, digest


@dataclass(frozen=True)
class Endpoint:
    name: str
    url: str
    key: str
    model: str


def _keys(*names: str) -> list[str]:
    values: list[str] = []
    for name in names:
        value = os.environ.get(name, "")
        if not value:
            continue
        for key in value.split(","):
            key = key.strip()
            if key and key not in values:
                values.append(key)
    return values


def configured_endpoints() -> list[Endpoint]:
    """Build the documented failover order without exposing credentials."""
    endpoints = [
        Endpoint("groq", "https://api.groq.com/openai/v1/chat/completions", key,
                 os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"))
        for key in _keys("GROQ_API_KEYS", "GROQ_API_KEY", "GROQ_API_KEY_2")
    ]
    endpoints.extend(
        Endpoint("openrouter", "https://openrouter.ai/api/v1/chat/completions", key,
                 os.environ.get("OPENROUTER_MODEL", "google/gemini-2.5-flash"))
        for key in _keys("OPENROUTER_API_KEY")
    )
    endpoints.extend(
        Endpoint("gemini", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", key,
                 os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"))
        for key in _keys("GEMINI_API_KEY")
    )
    return endpoints


def _openai_messages(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for message in messages:
        role, content = message["role"], message.get("content", "")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        if role == "assistant" and isinstance(content, list):
            calls = []
            text = []
            for block in content:
                if block.get("type") == "tool_use":
                    calls.append({"id": block["id"], "type": "function", "function": {
                        "name": block["name"], "arguments": canonical_json(block.get("input", {})),
                    }})
                elif block.get("type") == "text":
                    text.append(str(block.get("text", "")))
            item: dict[str, Any] = {"role": "assistant", "content": "\n".join(text) or None}
            if calls:
                item["tool_calls"] = calls
            out.append(item)
            continue
        if role == "user" and isinstance(content, list) and all(
                block.get("type") == "tool_result" for block in content):
            for block in content:
                out.append({"role": "tool", "tool_call_id": block["tool_use_id"],
                            "content": str(block.get("content", ""))})
            continue
        out.append({"role": role, "content": canonical_json(content)})
    return out


class RouterClient:
    """Presents the small Anthropic messages interface used by the harness."""

    def __init__(self, *, endpoints: list[Endpoint] | None = None, timeout: float = 90.0,
                 opener: Callable[..., Any] | None = None) -> None:
        self.endpoints = list(configured_endpoints() if endpoints is None else endpoints)
        if not self.endpoints:
            raise ValueError(
                "router backend needs GROQ_API_KEYS/GROQ_API_KEY, OPENROUTER_API_KEY, or GEMINI_API_KEY"
            )
        self.timeout = float(os.environ.get("ROUTER_TIMEOUT", str(timeout)))
        self._opener = opener or urllib.request.urlopen
        self.model = "router-chain"
        identity = [(endpoint.name, endpoint.url, endpoint.model) for endpoint in self.endpoints]
        self.base_url = "router:" + digest(identity)
        cache = os.environ.get("ROUTER_CACHE_DIR")
        self.cache_dir = Path(cache).resolve() if cache else Path(__file__).resolve().parents[1] / "out" / "router_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.messages = self

    def close(self) -> None:
        pass

    def _request(self, endpoint: Endpoint, payload: dict[str, Any]) -> dict[str, Any]:
        # Groq's edge rejects urllib's default Python user agent before inference.
        headers = {"Authorization": "Bearer " + endpoint.key, "Content-Type": "application/json",
                   "User-Agent": "nova-takehome/1.0"}
        if endpoint.name == "openrouter":
            headers["X-Title"] = "Nova freight audit local evaluation"
        request = urllib.request.Request(endpoint.url, data=canonical_json(payload).encode(), headers=headers, method="POST")
        with self._opener(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode())

    def create(self, *, system: str, messages: list, tools: list, max_tokens: int = 12000,
               **_: Any) -> anthropic.types.Message:
        if len(tools) != 1:
            raise ValueError("router transport expects exactly one data-return tool")
        tool = tools[0]
        schema = clean_schema(tool["input_schema"])
        validator = Draft202012Validator(schema)
        converted = _openai_messages(system, messages)
        request_identity = [self.base_url, tool["name"], schema, converted, max_tokens]
        cache_path = self.cache_dir / (digest(request_identity) + ".json")
        if cache_path.is_file():
            try:
                cached = anthropic.types.Message.model_validate_json(cache_path.read_text())
                payload = sanitize_payload(cached.content[0].input)
                validator.validate(payload)
                cached.content[0].input = payload
                return cached
            except (OSError, ValueError, IndexError, SchemaError):
                pass

        failures = []
        for endpoint in self.endpoints:
            body = {
                "model": endpoint.model,
                "messages": converted,
                "max_tokens": max_tokens,
                "temperature": 0,
                "tools": [{"type": "function", "function": {
                    "name": tool["name"], "description": tool.get("description", "Return structured data."),
                    "parameters": schema,
                }}],
                "tool_choice": {"type": "function", "function": {"name": tool["name"]}},
            }
            try:
                raw = self._request(endpoint, body)
                choice = raw["choices"][0]
                reply = choice["message"]
                calls = reply.get("tool_calls") or []
                if len(calls) == 1:
                    arguments = calls[0]["function"]["arguments"]
                    payload = arguments if isinstance(arguments, dict) else json.loads(arguments)
                else:
                    content = reply.get("content")
                    payload = content if isinstance(content, dict) else json.loads(content or "")
                payload = sanitize_payload(payload)
                if not isinstance(payload, dict):
                    raise TypeError("structured payload must be an object")
                try:
                    validator.validate(payload)
                    transport_valid = True
                except SchemaError:
                    # The established extractor/auditor layer owns the original
                    # Pydantic, evidence and calculation checks and its one repair.
                    # Return an object there, but never cache it before validation.
                    transport_valid = False
            except urllib.error.HTTPError as exc:
                failures.append(f"{endpoint.name}:http-{exc.code}")
                continue
            except (urllib.error.URLError, TimeoutError, socket.timeout):
                failures.append(f"{endpoint.name}:network")
                continue
            except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
                failures.append(f"{endpoint.name}:invalid-output")
                continue

            usage = raw.get("usage") or {}
            result = anthropic.types.Message.model_validate({
                "id": str(raw.get("id") or uuid.uuid4()), "type": "message", "role": "assistant",
                "model": str(raw.get("model") or endpoint.model),
                "content": [{"type": "tool_use", "id": str((calls[0].get("id") if calls else None) or "data_result"),
                             "name": tool["name"], "input": payload}],
                "stop_reason": "tool_use", "stop_sequence": None,
                "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                          "output_tokens": usage.get("completion_tokens", 0)},
            })
            if transport_valid:
                temporary = cache_path.with_suffix("." + uuid.uuid4().hex + ".tmp")
                try:
                    temporary.write_text(result.model_dump_json())
                    temporary.replace(cache_path)
                finally:
                    temporary.unlink(missing_ok=True)
            return result

        raise StructuredExtractionError("router providers exhausted (" + ", ".join(failures) + ")")

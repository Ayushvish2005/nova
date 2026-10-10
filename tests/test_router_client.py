from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from harness.router_client import Endpoint, RouterClient, _openai_messages, configured_endpoints
from harness.claude_extractor import StructuredExtractionError


class Response:
    def __init__(self, value: dict):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def read(self) -> bytes:
        return json.dumps(self.value).encode()


class RouterClientTests(unittest.TestCase):
    def test_environment_builds_key_failover_without_putting_keys_in_identity(self) -> None:
        env = {"GROQ_API_KEYS": "first,second", "OPENROUTER_API_KEY": "third", "GEMINI_API_KEY": "fourth"}
        with patch.dict(os.environ, env, clear=True):
            endpoints = configured_endpoints()
            self.assertEqual([e.name for e in endpoints], ["groq", "groq", "openrouter", "gemini"])
            with tempfile.TemporaryDirectory() as cache:
                os.environ["ROUTER_CACHE_DIR"] = cache
                client = RouterClient(endpoints=endpoints)
                self.assertNotIn("first", client.base_url)

    def test_rate_limit_uses_next_endpoint_and_validates_payload(self) -> None:
        calls = []

        def opener(request, timeout):
            calls.append((request.full_url, timeout, request.headers.get("Authorization")))
            self.assertEqual(request.headers.get("User-agent"), "nova-takehome/1.0")
            if len(calls) == 1:
                raise urllib.error.HTTPError(request.full_url, 429, "limited", {}, io.BytesIO())
            return Response({"id": "done", "model": "second", "choices": [{"message": {
                "tool_calls": [{"id": "call", "function": {"name": "return_value",
                                                               "arguments": '{"value":7}'}}]
            }}], "usage": {"prompt_tokens": 2, "completion_tokens": 3}})

        endpoints = [Endpoint("groq", "https://one.invalid", "secret-one", "m1"),
                     Endpoint("openrouter", "https://two.invalid", "secret-two", "m2")]
        with tempfile.TemporaryDirectory() as cache, patch.dict(os.environ, {"ROUTER_CACHE_DIR": cache}, clear=True):
            client = RouterClient(endpoints=endpoints, opener=opener)
            result = client.create(system="Return data", messages=[{"role": "user", "content": "seven"}],
                                   tools=[{"name": "return_value", "input_schema": {
                                       "type": "object", "properties": {"value": {"type": "integer"}},
                                       "required": ["value"], "additionalProperties": False,
                                   }}])
        self.assertEqual(result.content[0].input, {"value": 7})
        self.assertEqual([call[0] for call in calls], ["https://one.invalid", "https://two.invalid"])

    def test_repair_messages_convert_to_tool_protocol(self) -> None:
        converted = _openai_messages("rules", [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "x", "name": "save", "input": {"a": 1}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "is_error": True,
                                             "content": "fix it"}]},
        ])
        self.assertEqual(converted[1]["tool_calls"][0]["function"]["arguments"], '{"a":1}')
        self.assertEqual(converted[2], {"role": "tool", "tool_call_id": "x", "content": "fix it"})

    def test_invalid_object_is_returned_uncached_for_host_repair(self) -> None:
        def opener(request, timeout):
            return Response({"choices": [{"message": {"content": '{"value":"wrong"}'}}]})

        endpoints = [Endpoint("openrouter", "https://example.invalid", "credential", "model")]
        with tempfile.TemporaryDirectory() as cache, patch.dict(os.environ, {"ROUTER_CACHE_DIR": cache}, clear=True):
            client = RouterClient(endpoints=endpoints, opener=opener)
            result = client.create(system="Return data", messages=[], tools=[{"name": "return_value", "input_schema": {
                "type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"],
            }}])
            self.assertEqual(result.content[0].input, {"value": "wrong"})
            self.assertEqual(list(Path(cache).iterdir()), [])

    def test_exhausted_error_does_not_expose_credentials_or_response(self) -> None:
        def opener(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 401, "secret response body", {}, io.BytesIO())

        endpoints = [Endpoint("groq", "https://one.invalid", "credential-value", "m1")]
        with tempfile.TemporaryDirectory() as cache, patch.dict(os.environ, {"ROUTER_CACHE_DIR": cache}, clear=True):
            client = RouterClient(endpoints=endpoints, opener=opener)
            with self.assertRaisesRegex(StructuredExtractionError, r"groq:http-401") as raised:
                client.create(system="Return data", messages=[], tools=[{"name": "return_value", "input_schema": {
                    "type": "object", "properties": {}, "additionalProperties": False,
                }}])
        self.assertNotIn("credential-value", str(raised.exception))
        self.assertNotIn("secret response body", str(raised.exception))


if __name__ == "__main__":
    unittest.main()

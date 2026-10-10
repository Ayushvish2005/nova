"""The only place the harness talks to a model. Structured output, an on-disk cache keyed by content hash (a re-ingest
costs nothing), retries, a usage log for the cost estimate, and a provider chain with fallback.

Providers, tried in this order (override with HARNESS_PROVIDER=a,b,c):
  anthropic  ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL. This is what the grader runs: its container reaches api.anthropic.com
             and nothing else, so it is the only provider the final run can use.
  xkiro      XKIRO_API_KEY, OpenAI-compatible, Mistral / Qwen models. Development stand-in.
  groq       GROQ_API_KEY, OpenAI-compatible, gpt-oss. Development fallback (small free-tier limits).
Models are named by role ('fast' reads, 'main' extracts); each provider maps the role to its own model.
A repo-root .env (git-ignored) is read for any variable that is not already set. The model is a last resort: rule_parser reads
documents first, and HARNESS_NO_LLM=1 keeps the model out entirely.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def _load_dotenv() -> None:
    f = Path(__file__).resolve().parent.parent / ".env"
    if f.is_file():
        for line in f.read_text(encoding="utf-8").splitlines():
            k, sep, v = line.partition("=")
            if sep and not line.lstrip().startswith("#"):
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()
MODELS = {"anthropic": {"fast": "claude-haiku-5-5", "main": "claude-sonnet-5-5"},
          "xkiro": {"fast": "mistralai/mistral-small-2603", "main": "mistralai/mistral-large-2512"},
          "groq": {"fast": "openai/gpt-oss-20b", "main": "openai/gpt-oss-120b"}}
COMPAT = {"xkiro": {"url": "https://api.xkiro.com/v1/chat/completions", "key": "XKIRO_API_KEY"},
          "groq": {"url": "https://api.groq.com/openai/v1/chat/completions", "key": "GROQ_API_KEY"}}
KEYS = {"anthropic": "ANTHROPIC_API_KEY", "xkiro": "XKIRO_API_KEY", "groq": "GROQ_API_KEY"}


def providers() -> list[str]:
    want = [p.strip() for p in os.environ.get("HARNESS_PROVIDER", "").split(",") if p.strip()] or list(MODELS)
    return [p for p in want if os.environ.get(KEYS[p])]


PROVIDERS = providers()
FAST_MODEL, MAIN_MODEL = "fast", "main"          # roles, resolved per provider
WORKERS = int(os.environ.get("HARNESS_WORKERS", "8" if PROVIDERS[:1] == ["anthropic"] else "3"))
VISION = PROVIDERS[:1] == ["anthropic"]           # only Claude is sent page images; scans go through OCR otherwise


def model_for(provider: str, role: str) -> str:
    return os.environ.get(f"{provider.upper()}_{role.upper()}_MODEL") or MODELS[provider].get(role, role)


def image_block(png: bytes) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                        "data": base64.b64encode(png).decode()}}


def _strictify(schema):
    """Strict mode wants every object closed and every property required (optional = nullable, which ours are)."""
    if isinstance(schema, dict):
        out = {k: _strictify(v) for k, v in schema.items()}
        if isinstance(out.get("properties"), dict):
            out["additionalProperties"] = False
            out["required"] = list(out["properties"])
        return out
    if isinstance(schema, list):
        return [_strictify(v) for v in schema]
    return schema


class Transient(Exception):
    def __init__(self, msg, wait=0.0):
        super().__init__(msg)
        self.wait = wait


# Groq's free tier allows ~8,000 tokens a minute per model: spend them through one throttle shared by all threads.
GROQ_TPM = int(os.environ.get("GROQ_TPM", "7000"))
_win: list[tuple[float, int]] = []
_lock = threading.Lock()


def _throttle(est: int) -> None:
    while True:
        with _lock:
            now = time.time()
            _win[:] = [(t, n) for t, n in _win if now - t < 60]
            if sum(n for _, n in _win) + est <= GROQ_TPM or not _win:
                _win.append((now, est))
                return
            wait = 60 - (now - _win[0][0]) + 0.2
        time.sleep(max(wait, 0.5))


class LLM:
    def __init__(self, cache_dir: str | Path, client=None):
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._client = client              # an injected Anthropic-style client (tests) replaces the provider chain
        self.calls = 0                     # real API calls this process
        self.hits = 0                      # answered from the cache
        self.used: dict[str, int] = {}     # provider -> successful calls

    @property
    def client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(max_retries=0)       # we retry ourselves, in structured()
        return self._client

    # ---- providers ----------------------------------------------------------------------------------------------------
    def _anthropic(self, model, system, content, schema, name, max_tokens):
        try:
            r = self.client.messages.create(
                model=model, max_tokens=max_tokens, system=system, messages=[{"role": "user", "content": content}],
                tools=[{"name": name, "description": "Return the extracted record.", "input_schema": schema}],
                tool_choice={"type": "tool", "name": name})
        except Exception as e:
            if type(e).__name__ in ("RateLimitError", "APIConnectionError", "APITimeoutError", "InternalServerError",
                                    "OverloadedError") or getattr(e, "status_code", 0) in (429, 500, 502, 503, 529):
                raise Transient(str(e)) from e
            raise
        out = next(b.input for b in r.content if getattr(b, "type", "") == "tool_use")
        u = getattr(r, "usage", None)
        return out, getattr(u, "input_tokens", 0), getattr(u, "output_tokens", 0)

    def _compat(self, prov, model, system, content, schema, name, max_tokens):
        """An OpenAI-compatible chat endpoint with strict json_schema output (xkiro, groq)."""
        if not isinstance(content, str):
            if any(b.get("type") != "text" for b in content):
                raise ValueError(f"{prov} is text only: render scans with OCR")
            content = "\n".join(b["text"] for b in content)
        body = {"model": model, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
                "response_format": {"type": "json_schema",
                                    "json_schema": {"name": name, "strict": True, "schema": _strictify(schema)}}}
        if prov == "groq":
            _throttle(len(system + content) // 3 + max_tokens // 2)
            body["reasoning_effort"] = "low"
        cfg = COMPAT[prov]
        req = urllib.request.Request(cfg["url"], data=json.dumps(body).encode(), headers={
            "Authorization": "Bearer " + os.environ[cfg["key"]], "Content-Type": "application/json",
            "User-Agent": "nova-harness/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                j = json.load(r)
        except urllib.error.HTTPError as e:
            msg = e.read().decode()[:400]
            if (e.code in (429, 500, 502, 503) and "per day" not in msg) or "tool_use_failed" in msg or "json_validate_failed" in msg:
                raise Transient(f"{prov} {e.code}", float(e.headers.get("retry-after") or 0)) from e
            raise RuntimeError(f"{prov} {e.code}: {msg}") from e         # 413 too large, daily cap: retrying won't help
        except (urllib.error.URLError, TimeoutError) as e:
            raise Transient(str(e)) from e
        ch = j["choices"][0]
        try:
            out = json.loads(ch["message"]["content"])
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:    # empty or cut-off JSON
            if ch.get("finish_reason") == "length":
                raise RuntimeError("model output truncated: split the input or raise max_tokens") from e
            raise Transient(f"no usable JSON: {e}") from e
        u = j.get("usage") or {}
        return out, u.get("prompt_tokens", 0), u.get("completion_tokens", 0)

    # ---- the one entry point ------------------------------------------------------------------------------------------
    def structured(self, *, model: str, system: str, content, schema: dict, name: str = "record",
                   max_tokens: int = 8000) -> dict:
        """One JSON object matching `schema`. `model` is a role ('fast' / 'main'); `content` a string or content blocks.
        The first provider that answers wins; the rest are fallbacks."""
        chain = ["anthropic"] if self._client is not None else PROVIDERS
        if not chain:
            raise RuntimeError("no model provider configured (set ANTHROPIC_API_KEY, XKIRO_API_KEY or GROQ_API_KEY)")
        last: Exception | None = None
        for i, prov in enumerate(chain):
            mname = model_for(prov, model)
            key = hashlib.sha256(json.dumps([prov, mname, system, content, schema, name], sort_keys=True).encode()).hexdigest()
            f = self.dir / f"{key}.json"
            if f.is_file():
                self.hits += 1
                return json.loads(f.read_text(encoding="utf-8"))
            attempts, delay = (6 if i == len(chain) - 1 else 3), 2.0
            for attempt in range(attempts):
                try:
                    if prov == "anthropic":
                        out, tin, tout = self._anthropic(mname, system, content, schema, name, max_tokens)
                    else:
                        out, tin, tout = self._compat(prov, mname, system, content, schema, name, max_tokens)
                except Transient as e:
                    last = e
                    if attempt == attempts - 1:
                        break
                    time.sleep(max(e.wait, delay))
                    delay *= 2
                    continue
                except (RuntimeError, ValueError) as e:        # this provider cannot do it (cap, too large): try the next
                    last = e
                    break
                self.calls += 1
                self.used[prov] = self.used.get(prov, 0) + 1
                with (self.dir / "usage.jsonl").open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"provider": prov, "model": mname, "name": name, "in": tin, "out": tout}) + "\n")
                f.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
                return out
        raise RuntimeError(f"every model provider failed; last error: {last}")


def pmap(fn, items, workers: int = WORKERS) -> list:
    """Order-preserving parallel map; the first exception is raised after the rest finish."""
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        return list(ex.map(fn, items))

# Solution

## How to run it

Run `make setup`, then `make test`, `make grade`, `make eval`, `make sheet`, and
`make validate`. The grading entry point is `harness/harness.py`; it reads
`ANTHROPIC_API_KEY` and optionally `ANTHROPIC_BASE_URL`/`ANTHROPIC_MODEL`. All
Python dependencies are pinned. Local OCR uses the installed RapidOCR models;
no external OCR service is needed. Each CLI invocation reloads SQLite state.

The pipeline first parses recognized invoices and contracts locally. Grading uses
Claude only for declined documents and audit proposals. Local development can use
AGY explicitly:

```sh
make grade HARNESS='harness/agy_harness.py --agy-cache-dir /path/to/local-cache'
make eval HARNESS='harness/agy_harness.py --agy-cache-dir /path/to/local-cache'
make sheet HARNESS='harness/agy_harness.py --agy-cache-dir /path/to/local-cache'
```

There is no automatic provider fallback: `harness.py` always selects Anthropic and
`agy_harness.py` always selects the local AGY CLI. Keep response caches private;
the default cache under `out/` is ignored by version control.

For faster local cloud testing, `router_harness.py` explicitly enables an
environment-only chain of Groq keys, OpenRouter, then direct Gemini. OpenRouter
defaults to a Gemini model. The normal grading path never enters this chain.

```sh
export GROQ_API_KEYS='<rotated-key-1>,<rotated-key-2>'
export OPENROUTER_API_KEY='<rotated-key>'
# Optional direct Google endpoint; OpenRouter already routes Gemini by default.
export GEMINI_API_KEY='<rotated-key>'
make eval HARNESS='harness/router_harness.py --router-cache-dir out/router_cache'
```

Keys are never CLI arguments or cache content. The wrapper accepts
`--groq-model`, `--openrouter-model`, `--gemini-model`, and `--router-timeout`.
Every provider response passes the same local schema, citation, arithmetic and
safety checks. Failed or rate-limited endpoints advance to the next configured
credential; exhausted chains fail closed.

## What works

The implementation ingests MIME, PDF, XLSX and scanned invoices; preserves exact
document/page references; validates normalized Pydantic facts; and persists
versioned evidence, relationships, feedback and reports transactionally. Batch
replays are idempotent. The model interprets clauses; Python evaluates restricted
Decimal expressions, validates citations and scopes feedback, then applies action
gates. The supplied glossary is included in the trusted audit prompt.

The offline suite passes 128 tests; one opt-in Claude test is skipped without a key.
It covers ingestion, extraction, arithmetic, scoped feedback, action policy and
provider isolation, cache validation and timeout cleanup. A focused set of 26
deterministic-reader tests also passes.
There are 19 original scenario evaluations plus the two supplied samples, including
fictional carriers, reversed invoice columns, actual scanned PDFs, backdated
amendments, scoped feedback and prompt injection. The unfamiliar-layout scenario
passed end to end through the Python Codex SDK, recovering $11.00 with valid
citations and the expected action. A real structured SDK smoke call also passed.

Across the supplied practice and exam mailboxes, deterministic readers handle 55
of 246 unique documents without an LLM: 47 invoices, all four supplied contracts,
375 invoice lines, 836 contract terms/rates and 694 shipment facts. Each accepted
invoice has its line arithmetic, subtotal, tax, total, dates and source evidence
checked before facts are published to SQLite. Ambiguous or incomplete layouts are
declined as a whole and sent to the selected model.

Full grading remains incomplete. The current `report_card.*` and
`evals/results.json` were overwritten by runs without an Anthropic key or by slow
AGY calls that exceeded the grader's 600-second step limit; their zero scores do
not establish semantic accuracy. Regenerate all required artifacts with the final
Anthropic grading environment before submission.

## What doesn't

End-to-end Claude accuracy, the hidden grader and exam correctness have not been
verified against an answer key. A schema-valid local AGY CLI smoke call passed, but
a full AGY practice grade timed out because one fallback extraction and subsequent
reporting did not finish within the grader step budget. AGY is suitable for focused
local checks and cached iteration, not yet for a cold full run under that limit.
The Groq/OpenRouter/Gemini transport has mocked failover and schema-validation
coverage but has not been live-tested with a safe rotated credential.
Strict validation can reject an entire audit group after one repair;
large contract contexts currently fail visibly instead of being subdivided.
Semantic interpretation, applicability and model confidence remain fallible.

Confidence is bounded by supporting extraction confidence but is not empirically
calibrated. OCR may misread degraded scans. No email or payment integration exists:
`execute:*` represents a policy-approved, reversible intent in the outbox, with
`transport: not_sent`. Slow model calls can exceed the runner's step budget.

## The eval case that caught a bug

The fictional alternate-layout case exposed unsupported local-model schema constructs:
Pydantic discriminated `oneOf` unions and Decimal lookaround patterns were rejected
before inference. The adapter translates the transport schema to `anyOf`,
removes unsupported patterns, and still enforces the original Pydantic schema
locally. Its rerun correctly recovered $11.00 with exact citations.

Custom backdated tests also verify receipt-time filtering, revision lineage and
`retro`; regression tests cover split signature/effective-date extraction,
multi-month term applicability, feedback subject dates and cached outbox mode
switches. Offline tests use explicit typed proposals to isolate host behavior;
they are not presented as proof of model extraction accuracy.

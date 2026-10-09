# Solution

## How to run it

Run `make setup`, then `make test`, `make grade`, `make eval`, `make sheet`, and
`make validate`. The default entry point is `harness/harness.py`; it reads
`ANTHROPIC_API_KEY` and optionally `ANTHROPIC_BASE_URL` from the environment.
All Python dependencies are pinned. Local OCR uses the installed RapidOCR models;
no external OCR service is needed. Each CLI invocation reloads SQLite state.

This workspace has no Claude credentials. For local testing with an authenticated
Codex sign-in, explicitly select the optional SDK wrapper:

```sh
make grade HARNESS='harness/codex_harness.py --codex-home /path/to/codex-home --codex-cache-dir /path/to/local-cache'
make eval HARNESS='harness/codex_harness.py --codex-cache-dir /path/to/local-cache'
make sheet HARNESS='harness/codex_harness.py --codex-cache-dir /path/to/local-cache'
```

The optional wrapper uses `openai-codex==0.162.0`, installed by `make setup` with
its pinned runtime, and reads your existing Codex sign-in. The hidden grader uses
the Claude default. Keep the local response cache private and outside version control.

## What works

The implementation ingests MIME, PDF, XLSX and scanned invoices; preserves exact
document/page references; validates normalized Pydantic facts; and persists
versioned evidence, relationships, feedback and reports transactionally. Batch
replays are idempotent. The model interprets clauses; Python evaluates restricted
Decimal expressions, validates citations and scopes feedback, then applies action
gates. The supplied glossary is included in the trusted audit prompt.

The offline suite passes 97 tests; one opt-in Claude test is skipped without a key.
It covers ingestion, extraction, arithmetic, scoped feedback, action policy and
SDK isolation, cache validation and timeout cleanup.
There are 19 original scenario evaluations plus the two supplied samples, including
fictional carriers, reversed invoice columns, actual scanned PDFs, backdated
amendments, scoped feedback and prompt injection. The unfamiliar-layout scenario
passed end to end through the Python Codex SDK, recovering $11.00 with valid
citations and the expected action. A real structured SDK smoke call also passed.
Full grading remains incomplete: current practice artifacts report zero
after CLI timeouts/incomplete extraction; they do not establish semantic accuracy.
Final results must be checked before submission.

## What doesn't

Live Claude accuracy, the hidden grader and exam correctness have not been verified
without credentials or an answer key. Local Codex runs do not establish Claude
equivalence. Strict validation can reject an entire audit group after one repair;
large contract contexts currently fail visibly instead of being subdivided.
Semantic interpretation, applicability and model confidence remain fallible.

Confidence is bounded by supporting extraction confidence but is not empirically
calibrated. OCR may misread degraded scans. No email or payment integration exists:
`execute:*` represents a policy-approved, reversible intent in the outbox, with
`transport: not_sent`. Slow model calls can exceed the runner's step budget.

## The eval case that caught a bug

The fictional alternate-layout case exposed unsupported Codex schema constructs:
Pydantic discriminated `oneOf` unions and Decimal lookaround patterns were rejected
before inference. The optional adapter translates the transport schema to `anyOf`,
removes unsupported patterns, and still enforces the original Pydantic schema
locally. Both its initial CLI rerun and its SDK rerun correctly recovered $11.00.

Custom backdated tests also verify receipt-time filtering, revision lineage and
`retro`; regression tests cover split signature/effective-date extraction,
multi-month term applicability, feedback subject dates and cached outbox mode
switches. Offline tests use explicit typed proposals to isolate host behavior;
they are not presented as proof of model extraction accuracy.

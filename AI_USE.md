# AI use

This implementation and write-up were produced collaboratively with an AI coding
assistant. No claim of unaided authorship or unavailable live evaluation is made.

## Tools

OpenAI Codex was used to inspect the repository, propose architecture, implement
the harness, write tests, diagnose failures and draft documentation. The locally
installed Codex CLI (0.161.0, authenticated through ChatGPT) was used for initial
runtime checks. At the user's request, the optional extraction/audit backend now
uses the Python Codex SDK (`openai-codex==0.162.0`) and its matching pinned runtime.
Its configured default model is used; the adapter does not pin or claim a specific
underlying model snapshot.

The submission's default runtime uses Anthropic Claude Sonnet 4.6 through the
Anthropic SDK, configurable by environment. Live Claude tests were not run because
this workspace has no Anthropic key. The optional Codex backend is for explicit
local testing and is not assumed available inside the hidden grader.

## How you worked with them

The user requested incremental implementation: ingestion/persistence, structured
extraction, feedback/reporting, then generalization tests and grading. Codex read
the brief, glossary, grader and examples; developed schema-driven modules; and
ran offline checks, initial live CLI checks, an SDK structured-output smoke
test and a passing SDK unfamiliar-layout scenario. It added fictional unseen-layout/OCR/backdated/
injection/feedback mailboxes, checked citations with grader utilities and tested
idempotence across fresh processes. Runtime code does not load public answers.

Initial Codex schema calls failed on unsupported union/Decimal patterns; the
transport adapter was corrected and its real scenario rerun passed. Regression
tests exposed brittle safety date joins and cached-outbox behavior, which were
fixed at host boundaries. Assertions using mocked proposals test implementation,
not model accuracy. Human review of every output has not been performed, and
the documentation explicitly separates missing credentials from semantic failure.

## Written without AI

The user supplied the requirements and accepted the architecture and implementation
sequence. Repository briefs, grader and supplied fixtures predate this work. The
new code, tests and these documentation changes are AI-assisted; no additional
unaided writing is claimed on the user's behalf.

## Prompt log

The available visible conversation and tool log is exported to
[ai_log/session.jsonl](ai_log/session.jsonl) by `tools/export_ai_log.py`. It includes
user messages, visible assistant updates and tool calls/results from the supplied
session, with credential-shaped values redacted. Private internal reasoning,
system instructions and runtime metadata are not part of this chat export.
The exporter reads only the explicitly supplied session, not other user histories.
The export is a snapshot refreshed during completion; model outputs are also
retained in private SQLite extraction attempts/local caches, not fabricated here.

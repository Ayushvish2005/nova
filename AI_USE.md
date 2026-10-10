# AI use

This implementation and write-up were produced collaboratively with an AI coding
assistant. No claim of unaided authorship or unavailable live evaluation is made.

## Tools

OpenAI Codex was used to inspect the repository, propose architecture, implement
the harness, write tests, diagnose failures and draft documentation. The locally
installed Codex CLI and Python SDK were used for earlier structured-output runtime
experiments. Those experiments are recorded in the exported log; they are not the
selected local runtime for the final workflow.

The grading runtime uses Anthropic Claude for documents that fail closed local
parsing and for semantic audit proposals. Local development can explicitly use the
authenticated AGY CLI; a schema-valid AGY smoke call was run. The normal grader
entry point still reads the grader-provided `ANTHROPIC_API_KEY`. The normal grading
and AGY wrappers never switch providers automatically.

After credentials were pasted into the visible chat, Codex treated them as
compromised and did not use or store them. It added an explicit local cloud wrapper
that reads replacement Groq, OpenRouter and Gemini credentials from environment
variables. Its configured chain may fail over between those providers; the normal
Anthropic grader entry point remains isolated from it. Tests use fake credentials
and mocked HTTP responses only.

## How you worked with them

The user requested incremental implementation: ingestion/persistence, structured
extraction, feedback/reporting, then generalization tests and grading. Codex read
the brief, glossary, grader and examples; developed schema-driven modules; and
ran offline checks, initial live CLI checks, an SDK structured-output smoke
test and a passing unfamiliar-layout scenario. It added fictional unseen-layout/OCR/backdated/
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
session through 9 October 2026, with credential-shaped values redacted. Private internal reasoning,
system instructions and runtime metadata are not part of this chat export.
The exporter reads only the explicitly supplied session, not other user histories.
Model outputs are also
retained in private SQLite extraction attempts/local caches, not fabricated here.

Continuation prompts after that export, reproduced from the visible conversation:

- “Write the ai log in the Readme format and is the assignment ready to be submit.”
- “Make the current changes in another branch and move the main branch to commit
  `3da037852eff2121f206c927001dcb702631a892`.”
- “Tell me the current architecture.”
- “Make extraction faster and reduce LLM usage. First try rule-based parsing for
  invoices, rates, and contracts, validating calculations before saving to SQLite.
  Use an LLM only for complex or unclear cases, and verify results against source.”
- “The grading will use the Anthropic API key but use AGY locally only.”
- “Run `make test`, `make grade`, and `make eval`.”
- “Use the AGY CLI instead of Claude API for local LLM calls.”
- “Use Groq, OpenRouter, and Gemini keys; use a second Groq key if the first is
  rate-limited.” (The credential values are intentionally omitted and must be
  revoked because they were disclosed in chat.)
- “Resume.” / “Why is it taking so much time?” / “Update the docs needed to submit
  the assignment.”

Codex implemented the deterministic readers and AGY adapter, measured reader
coverage, ran the offline suite, attempted the requested grade/eval commands, and
documented that cold AGY runs can exceed the grader timeout. No credentials are
included in this continuation log.

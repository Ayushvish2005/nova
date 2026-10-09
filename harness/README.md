# Harness: ingestion, extraction, feedback and reporting

The grader's three commands are implemented. Claude interprets documents and
proposes source-backed audit calculations; Python validates provenance, evaluates
Decimal arithmetic, scopes human feedback, and chooses actions. No email transport
or payment integration is enabled: actions are recorded in a SQLite outbox.

```sh
make setup
make test
.venv/bin/python harness/harness.py ingest data/scenarios/batch_01 --state-dir .state
.venv/bin/python harness/harness.py feedback data/scenarios/feedback/W1_batch_01.json --state-dir .state
.venv/bin/python harness/harness.py report --as-of 2035-06-30 --mode supervised --state-dir .state
```

Set `ANTHROPIC_API_KEY` in your shell before normal ingestion/reporting. The SDK
honors `ANTHROPIC_BASE_URL`; neither value is logged. `ANTHROPIC_MODEL` optionally
overrides the default `claude-sonnet-4-6`. Never put credentials in repository files.
Ingest/extract also accept `--model` and bounded `--workers` (default 4).

For local development without an Anthropic key, the optional `codex_harness.py`
wrapper uses the pinned `openai-codex` Python SDK, installed by `make setup` with
its matching app-server runtime. Each request starts an ephemeral, read-only SDK
thread in an empty directory with action tools disabled and approval mode set to
`deny_all`. Source messages use the SDK's `ExternalMessage` input, which has tool
authority below the trusted extraction instructions. Structured JSON passes the
same Pydantic, evidence and arithmetic validators. An existing Codex sign-in is
required; `codex exec` and a separately installed CLI are not used by this adapter.

```sh
make eval HARNESS='harness/codex_harness.py --codex-cache-dir /absolute/private/cache'
make grade HARNESS='harness/codex_harness.py --codex-home /absolute/codex/home --codex-cache-dir /absolute/private/cache'
make sheet HARNESS='harness/codex_harness.py --codex-cache-dir /absolute/private/cache'
```

`--codex-home` explicitly locates your existing Codex login when the local grader
creates a fresh HOME. Never copy login files into the repo. `CODEX_MODEL` can select
a model; otherwise the configured default is used. The private response cache is
opt-in, keyed by SDK/runtime versions, model, effort, user-configuration fingerprint,
schema and inputs; cached
responses are still validated. Old CLI cache entries have separate identities.
Codex extraction uses at most 1,800 source characters and one original page per
call to bound strict-output latency. Dense pages are split without dropping text,
changing original page numbers or rounding source values.
`--codex-reasoning-effort` optionally sets a supported SDK effort level and is
included in cache identity; no effort override is applied by default.
There is no automatic provider fallback; normal `harness.py` still uses Claude.
Requests have a 180-second deadline. On timeout the adapter interrupts the turn
and closes its owned SDK runtime. Document workers default to eight in this
wrapper, with at most three concurrent pages per long document.

To read documents without an API call, add `--documents-only` to `ingest`. Later run
`python harness/harness.py extract --state-dir .state [--batch batch_01]`. Raw
ingestion remains committed if structured extraction fails. Reports reject
incomplete extraction instead of presenting an empty report as a clean audit.

## Modules and records

- `readers.py`: MIME discovery and EML/PDF/XLSX extraction through `grader.extract`.
- `ocr.py`: lazy, local CPU OCR with an injectable engine; bundled RapidOCR models.
- `models.py` and `schema/document.schema.json`: validated source, page, and extraction records.
- `store.py` and `schema/state.sql`: transactional SQLite storage in `<state-dir>/state.sqlite3`.
- `ingestion.py`: batch orchestration, caching, and retry policy.
- `facts.py`: Pydantic invoice/header/line, contract, amendment, clause, authority,
  waiver, shipment, email and payment models; unknown values remain null.
- `claude_extractor.py`, `fact_validation.py`, `structured_ingestion.py`: bounded
  source chunks, schema/evidence validation, one repair attempt, concurrent API calls.
- `fact_store.py`: immutable-source fact versions, active versions and relationship indexes.
- `feedback.py`: idempotent feedback receipt and exact-key/conjunctive scope checks.
- `auditor.py`, `audit_models.py`, `calculations.py`: glossary-driven proposals and
  restricted Decimal expression evaluation over held fact fields (never `eval`).
- `audit_context.py`, `actions.py`, `reporting.py`: as-of evidence, fail-closed safety
  checks, grader-schema validation, revision history and action outbox.

Every email and attachment is a separate source occurrence. Evidence references
remain `<batch>/<email>.eml#<exact attachment filename>` (without the suffix for
emails); pages are one-based, including hidden Excel sheets. Raw bytes, MIME and
email metadata, UTC receipt dates, page text, and OCR boxes/confidence are retained.
There are no customer, carrier, or invoice-layout templates.

`native_text` is exactly what the grader extracts. `text` normally matches it,
but contains OCR for image-heavy/scanned PDF pages, or readable HTML for HTML-only
emails. OCR runs per page, including mixed PDFs. Do not treat OCR/HTML text as a
verbatim native-text quote: the grader only relaxes text checking for wholly
image-only PDFs. Region coordinates use rendered-image pixels; page dimensions
are stored with them.

## Persistence and failure behavior

`documents` stores receipt identity; `blobs`, `extractions`, and `pages` share
content by SHA-256, format, and reader version. Re-ingesting unchanged input adds
no records and reuses successful extraction. Resends retain distinct evidence
references even when their bytes match. Reader/configuration upgrades create new
extraction versions without duplicating source occurrences.

Each batch is one transaction. Changed bytes at an existing reference, ambiguous
attachment filenames, or invalid receipt dates reject the batch without partial
document writes. Corrupt or unsupported attachments retain their original bytes
with explicit `failed`/`unsupported` statuses; other documents are still stored,
and the CLI returns nonzero with errors on stderr. Failed extraction is retried
on the next ingest; unsupported formats require a reader upgrade. Reports must
eventually surface these failures rather than assume missing evidence is absent.

The database is self-contained after ingestion. Use `StateStore.get_document`,
`raw_bytes`, or `documents(as_of="2026-06-01")` to retrieve persisted evidence.
SQLite migrations add fact storage (v2), then feedback/reporting (v3), without
replacing v1 documents. Unknown future schema versions fail explicitly.
Only input documents are read: scenario manifests, answers, and canary headers are
not used as extracted evidence. Attachment filenames are never filesystem paths.

## Structured facts and relationships

Claude receives only ingested text/metadata, never answer files. The complete
Pydantic schema is supplied as a data-return tool and enforced locally, including
unknown-field rejection, real calendar dates, finite decimals and exact quotes.
Using a non-strict tool avoids the API's combined optional/union-field limits for
this heterogeneous schema; invalid payloads get one bounded repair, not acceptance.
See [Claude tool definitions](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools)
and [schema limits](https://platform.claude.com/docs/en/build-with-claude/structured-outputs).

Dates retain both raw wording and ISO values, separately from receipt timestamps.
Financial values use Decimal strings without extraction-time rounding. Invoice
headers and lines are independent records joined by normalized invoice ID; the
printed number remains intact. Relationships retain explicit contract, shipment,
master/house and document references; unresolved named documents have no guessed
path. Resends remain separate source occurrences, not merged invoices by filename.

Only complete validated document fact sets become active. Failed versions remain
visible and retryable, with no partial facts exposed. Model, prompt/schema, reader,
input and configuration changes invalidate caches. Inspect facts with
`FactStore.facts(as_of=...)`, IDs with `fact_records()`, and errors/issues with
`status(doc_ref)`. Model responses and token counts remain in the state database.

## Feedback and actions

Feedback is immutable by `(world, item_id)` and counts only after `decided_at` in
UTC. One-off decisions match the exact finding key. Standing claims require every
filter and the covered subject date; a close never closes later invoices. Original
feedback, actor, scope and provenance are stored. Changed feedback IDs are rejected;
send corrections as new items. Separate worlds require separate state directories.

The host aggregates outbound amounts across classes on an invoice; exactly $5,000
requires review. Supervised mode never executes outbound actions. Autonomous mode
requires complete evidence, applicable terms, sufficient authority, supported pickup
dates, a Schedule 1 recipient and no overlapping open conflict. Unknown gates hold
for review. One-off approval never bypasses these gates. Missing-document requests
are conservatively held in autonomous mode under the glossary's complete-evidence rule.

Reports use the grader's JSON schema, checked quotes, stable semantic revision IDs,
`supersedes`, `retro` and `learned_at`. Confidence is capped by the supporting facts'
extraction confidence; it is **not empirically calibrated yet**. Changed/withdrawn
draft or ready actions become superseded. Repeated unchanged actions share an
outbox ID, even across report dates. `execute:*` means a policy-approved outbox
record with `transport: not_sent`, not that an external message was delivered.

## Tests and limitations

`make test` is offline by default and covers real example documents with mocked
Claude outputs plus local OCR, scope isolation, arithmetic, action gates, migrations,
reruns, schema/citations and CLI behavior. It does not measure live model accuracy.
`RUN_CLAUDE_TESTS=1 make test` opts into paid live extraction smoke tests when a key
is configured. Run `make eval` and `make grade` with credentials for end-to-end
evaluation. Clause interpretation and coverage still need that evaluation; a valid
quote proves provenance, not that the model interpreted the clause correctly.

Large audit groups fail explicitly at the context limit instead of truncating
evidence. Invoice targets are partitioned into groups of at most four, while every
group retains the full related historical context for duplicates and credits;
portfolio evaluation runs separately for larger groups. Null/default-only fields
are compacted without changing array indexes or source values. There is no semantic
retrieval index for oversized input histories; that and calibration are future work.

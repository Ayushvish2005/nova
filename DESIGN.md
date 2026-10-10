# Design

Documents are immutable evidence; extracted claims are versioned interpretations.
Fail-closed Python readers handle complete recognized invoice and contract layouts.
Claude proposes meaning for declined documents and source-backed audit expressions
during grading; the explicit local wrapper uses AGY instead. Python owns validation,
feedback scope, calculations, report identity and action policy.

## Fact model

SQLite stores content-addressed original bytes, receipt-time source records,
reader versions, native/OCR pages, extraction attempts, active fact versions and
relationship indexes. Source keys are mailbox-relative references, including
`batch_01/0001.eml#attachment.pdf`; identical bytes can have different receipts.
Pydantic models represent invoice headers/lines, contracts, amendments, terms,
authority, waivers, shipments, emails and payments. Every claim carries a verbatim
quote, original page and extraction layer. Dates retain raw wording and nullable
ISO interpretation; quantities and money use Decimal. Ambiguity is recorded rather
than silently filled. Publishing a document's facts is atomic and replay-safe.

## Conflicts

The audit model reads held claims and the glossary, proposing deciding source IDs,
affected scopes and two recovery expressions when trusted sources disagree.
Python requires available evidence and two authoritative, in-force deciding
documents for conflicts. Unsigned/quoted claims cannot establish equal authority.
Precedence interpretation remains semantic, not a carrier-specific rule table.
Conflict scopes block autonomous actions only on overlapping contract, lane,
charge and period; uncertain overlap fails closed. Missing-document requests need
the exact cited name and a category-appropriate contact from a held Schedule 1.

## Confidence

The model estimates joint verdict/amount confidence from completeness, authority,
agreement and extraction quality. The host caps it at the lowest confidence of
all declared supporting facts. Invalid evidence is rejected, not merely discounted;
authority and applicability also independently gate actions. This is a conservative
heuristic, not a calibrated probability. Offline tests check its bounds and
supporting-fact propagation. A meaningful held-out Brier/AUROC calibration study
has not yet been completed and is required before production use.

## People in the loop

Supervised mode drafts outbound remedies; internal expiry, payment and credit
tracking actions remain internal. Conflicts escalate. Aggregate outbound recovery
of $5,000 or more per invoice/cashback key requires human review in either mode.
Autonomous intents additionally require complete evidence, in-force authoritative
terms, reliable pickup evidence, no overlapping conflict and a Schedule 1 recipient.
Only disputes, credit claims and document requests are permitted. Payments,
settlement and accepting terms are unavailable. The idempotent outbox records
intents only: there is no live outbound transport.

## Feedback

Feedback is immutable by world/item ID and available only after its decision
timestamp. Close/reject/amount override affect the exact finding key. Standing
facts require every declared applicability filter and covered subject-date interval;
one-off facts never become permanent authority. Unknown filters fail closed.
Semantic feedback lists its used IDs, which the host verifies against scope.
Later signed amendments can supersede overlapping feedback. Revised items retain
`learned_at` provenance. Free-text notes do not grant permission to execute.

## New documents

New batches append sources; receipt time and term effective time are separate.
Reports select only documents available by end-of-day UTC. Models read headings,
tables and wording without carrier/layout dispatch. Local OCR supplements scanned
pages while preserving native text. New source/schema/prompt/provider versions
invalidate cached interpretations. Changed verdicts, amounts or deadlines receive
stable semantic IDs and `supersedes`; a first finding caused by a backdated
amendment gets `retro`. Obsolete pending outbox intents are superseded; historical
reports never reactivate an old current action.

## Cost

Recognized invoices, rate tables and contracts require no model call. In the supplied
corpus, local readers cover 55/246 documents (22.4%), including 47 invoices and all
four contracts. At the same document mix, about 776 of every 1,000 documents would
need fallback extraction. Model fallback uses bounded chunks with up to four
concurrent documents. AGY extraction requests use at most 1,800 source characters
and one page per request. All providers allow one validation repair. Audits group by relationships
and run up to four groups concurrently. SQLite caches validated extractions and
assessments across processes; local AGY responses are content-addressed for replays.
The explicit local cloud wrapper similarly caches validated responses and uses a
declared Groq-to-OpenRouter-to-Gemini chain; the grading entry point stays on Claude.
Extraction attempts record token counts when the provider supplies them.

A planning estimate at illustrative Sonnet pricing of $3/M input and $15/M output,
assuming 10k input and 1.5k output tokens per fallback, is about $41 per 1,000
documents for extraction. Assuming this corpus's invoice density, four invoice
targets per audit and 40k input/2k output per audit adds about $7, for roughly
$48/1,000 before repairs. This is not a measured bill: schema size, document mix,
audit history and repair rate dominate, and a repair can repeat a call. Production
budgeting must replace these assumptions with provider usage logs and observed
p50/p95 cost. Audit payloads compact optional fields but retain related history;
groups over 350,000 characters fail closed rather than silently truncating.

## What I'd do next

Final `make grade` and `make eval` artifacts could not be produced locally. The
default route needs `ANTHROPIC_API_KEY`, which was unavailable. AGY exceeded the
600-second step limit and produced oversized or invalid reports. Groq rejected full
schemas (HTTP 400), then rate-limited both keys (HTTP 429); OpenRouter/Gemini failed
strict schema validation. Current zero scores are failed-run diagnostics, not
accuracy measurements. Regenerate them with the grader's Anthropic key.

Then measure reader coverage, fallback calls, latency and accuracy separately on
practice, custom and held-out data. Add bounded hierarchical audit
contexts that retain duplicate/rebate relationships, source-level applicability
checks and independently derived deadlines. Benchmark tokens, latency and repairs;
calibrate confidence on wrong as well as correct outcomes. Improve OCR diagnostics
and targeted retries, then add a separately approved transport with delivery
receipts and reconciliation. The largest uncertainty is semantic clause selection
and arithmetic-expression construction on long, conflicting document histories.

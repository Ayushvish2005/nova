# Design

Documents are immutable evidence; extracted claims are versioned interpretations.
The model proposes meaning and source-backed arithmetic, while Python owns
validation, feedback scope, calculations, report identity and action policy.

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

Extraction uses at most 10,000 characters/two pages per request and four concurrent
documents with Claude. The optional Codex SDK wrapper uses at most 1,800 characters
and one page per request,
eight document workers and up to three parallel pages per document. Both allow
one validation repair. Audits group by contract/invoice relationships
and run up to four groups concurrently. SQLite caches validated extractions and
assessments across processes; an optional private Codex response cache helps local
replays. Extraction attempts record input/output tokens. No credible measured
1,000-document Claude benchmark exists without a key. Budget using
`input_tokens * input_price + output_tokens * output_price`, plus audit/repair
requests, with prices expressed per token. Audit requests target at most four
invoices but retain related history; optional empty fields are compacted. A small
local Codex scenario took about 90 seconds through the earlier CLI adapter and
about four minutes through the SDK at the configured high effort. Extrapolation
would be unreliable because document length,
model latency, concurrency and cache hit rate dominate. Subscription Codex usage is
not evidence of API dollar cost. Large audit groups currently stop at 350,000
characters instead of implementing semantic retrieval.

## What I'd do next

First run the unchanged default backend with Claude credentials and measure
practice, custom and held-out accuracy separately. Add bounded hierarchical audit
contexts that retain duplicate/rebate relationships, source-level applicability
checks and independently derived deadlines. Benchmark tokens, latency and repairs;
calibrate confidence on wrong as well as correct outcomes. Improve OCR diagnostics
and targeted retries, then add a separately approved transport with delivery
receipts and reconciliation. The largest uncertainty is semantic clause selection
and arithmetic-expression construction on long, conflicting document histories.

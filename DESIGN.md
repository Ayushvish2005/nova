# System Design

## Fact model

The audit system maintains an immutable fact store partitioned by document identifier (e.g. `batch_01/0014.eml#INV.pdf`) within `--state-dir`. Each extracted fact is stamped with page indices, line references, document reception timestamps (`received_at`), and semantic classifications. Contracts are normalized into baseline terms (payment credit periods, late interest schedules, volume rebate structures, annual credits), lane-specific rate matrix records, fuel surcharges, fee definitions, Annex E charge codes, Annex F currency schedules, and Schedule 1 authorized representatives. Invoices break down into individual line items; operational tracking logs provide physical handling and delivery events; AP ledgers yield payment execution state; and correspondence records capture assertions regarding waivers, extensions, or rate adjustments. Citation quotes are strictly grounded by referencing source page and line bounds, preventing generative hallucinations. Dual timeline tracking isolates reception availability (`received_at <= as_of`) from contractual enforceability on shipment execution dates (`effective_from/to`).

Extraction employs a deterministic-first engine. The parser dynamically detects invoice tables, maps column headers, and mathematically validates line arithmetic ($quantity \times rate = amount$). Contractual clauses are identified by semantic keyword families rather than fixed numbering. If arithmetic does not balance or an ambiguous condition arises, the system defers to an LLM fallback where returned values are verified against line citations. Scanned files undergo local OCR prior to table extraction.

## Conflicts

For any given consignment, the audit engine aggregates all applicable rate definitions active as of the pickup timestamp: the Master Services Agreement (MSA), executed amendments, referenced annex revisions, and authorized carrier rate notices from designated Schedule 1 personnel. Unverified external senders and quoted forwarded bodies cannot modify commercial terms. Where multiple authorized sources establish differing rates without explicit contractual ranking (clause 13.3), the finding is classified as a `conflict`. The system records both competing instruments and computes the dispute interval $[\max(0, \text{billed} - \text{higher}), \text{billed} - \text{lower}]$, escalating the matter for manual review. When documents reference unheld instruments (such as an unattached revised annex) or unlisted exchange rates in Annex F, the item is designated as `cannot_determine` alongside the designated custodian from Schedule 1.

## Confidence

Scoring begins with an initial baseline of 0.95, decrementing systematically based on evidence vulnerability: pickup timestamps inferred solely from invoices or AWBs rather than physical milestone tracking (-0.05 to -0.10); table line arithmetic discrepancies (-0.15); presence of unauthorized carrier correspondence (-0.05); or reliance on OCR text (-0.05). Unresolved conflicts and indeterminable citations default to a fixed 0.88 baseline. This deterministic penalty framework prioritizes high-certainty operational telemetry over self-reported billing statements while preserving transparency.

## People in the loop

In supervised operating mode, internal remedial operations (accounting alerts, payment reminders, credit balance updates) proceed automatically, whereas outbound financial recoveries remain in draft status. Individual line or cumulative invoice disputes exceeding $5,000.00 require human review across all modes; contractual conflicts trigger escalation. In autonomous mode, outbound actions are marked for direct execution (`execute:*`) only when five strict automated criteria are satisfied: verifiable pickup dates from tracking records, absence of active rate conflicts across the contract period and lane, claim exposure strictly under $5,000.00, financial reversibility of the remedy, and verified recipient credentials from Schedule 1. Inbound prompt injections or text instructing automated systems are sanitized and ignored.

## Feedback

Human feedback entries are stored immutably with timestamped provenance and take effect from their recorded decision time (`decided_at`). Standing approvals require an exact conjunctive match across all declared filters (carrier contract, transport lane, authorized representative, charge category) within the valid window; one-off rulings remain strictly isolated to the specified invoice finding key. Ratified rate notices update the corresponding active tariff; verified representative delegations establish category authorization by name. Subsequent report generation records `learned_at` markers on findings altered by applied feedback.

## New documents

The reporting pipeline operates as a pure deterministic function over state facts as of `--as-of`, guaranteeing idempotence regardless of document ingestion sequence. State tracking compares successive report snapshots: findings whose status, calculated amounts, or deadlines evolve reference their prior state via `supersedes`, while adjustments triggered retroactively by backdated amendments or delayed notices are flagged as `retro`. Completed payments, settlement credits, or formal validity extensions retire obsolete alerts cleanly.

## Cost

By leveraging deterministic parsing for standard invoice layouts, tables, and contract instruments, the system processes both practice and examination mailboxes with 0 model tokens, achieving $0.00 operational API overhead for standard document batches. When routing ambiguous or irregular layouts to LLM fallback extraction, consumption averages ~1.0k input and ~0.9k output tokens per invoice, and ~1.9k input tokens per contract section chunk. Processing throughput achieves sub-second latency for native text documents, with scanned visual pages handled by local CPU OCR.

## What I'd do next

Future enhancements include implementing non-linear step-down margin rebate calculations once appropriate operational training data is available. Expanding the heuristic pattern library for varied international email phrasing would further minimize fallback invocation. Furthermore, fitting the confidence weights against an empirical error dataset and adding native multi-platform compilation targets would enhance cross-environment deployability.

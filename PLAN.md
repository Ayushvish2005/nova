# Plan: Nova freight-audit harness

## Principle

**The LLM reads. Python decides.** The LLM only turns documents into structured facts with source citations. It never
does arithmetic, applies rules or picks actions. Everything downstream is deterministic code that follows GLOSSARY.md.
`report` makes no LLM calls: fast, free to re-run, independent of file order.

## Architecture

```
ingest(batch)                                   report(as_of, mode)                 feedback(file)
 .eml ─► docreader ─► page text (+vision          facts ≤ as_of ─► authority/         decisions ─► state/feedback.json
         for image-only PDFs)                      │               precedence          (scoped facts, level 2)
            │                                      ▼                  │
            ▼                                   pricing ──► detectors (6 classes)
   classify doc type (small model)                  │            │
            │                                       ▼            ▼
   typed extractor (LLM, JSON schema)        conflict / cannot_determine
            │                                       │
   grounding check (numbers + quotes                ▼
   must appear in the page text)             action policy (mode, $5k, 5 checks)
            │                                       │
            ▼                                       ▼
   state/facts/*.json (with provenance)      confidence ─► evidence ─► report.json
                                                    │
                                                    └─► ledger of prior reports (supersedes / retro / stale)
```

Two time axes: `received_at` decides whether a document exists as of D; `effective_from/to` decides whether a clause
was in force on the pickup date. A backdated amendment is where they differ.

### Facts (plain JSON under the state dir; each carries doc, page, line_refs, received_at, authority level)

Contract (term, renewal, precedence, pricing-date rule) · rate card (lane x band x period, fuel by month, accessorials,
Annex E label map, Annex F FX) · Schedule 1 · credit and fee clauses (rebate tiers, QS definition, step-down, annual
credit, late fee r/grace/compounding, extension, waiver) · amendment · rate notice · invoice / credit note / Excel
statement · records (tracking, AWB, DGD, AP register) · email assertions (waiver, extension, picked-up, forwarded flag,
`directive_to_ai` flag) · references (documents a document cites: drives cannot_determine) · feedback facts.

### Modules

```
harness/  harness.py  docreader.py  llm.py  extraction.py  facts.py  authority.py
          pricing.py  detectors.py  policy.py  reporter.py  feedback.py
evals/    make_mailbox.py  cases/*.yaml
```

### Decisions

- Quotes are never LLM-generated: extractors see numbered lines and return line numbers; the harness copies the text.
- Grounding check per extraction: every number appears in the page, qty x rate = amount, lines sum to total. One retry
  with the failure named, else flag low confidence.
- Injection: email text is delimited data to a schema-only extractor with no tools. Actions come from code alone.
- Scans: detect image-only PDFs, render with pymupdf, extract with Claude vision; rapidocr as fallback.
- Cost: small model classifies, stronger model extracts; on-disk cache by content hash; parallel calls.
- Feedback: scoped facts; every `applies_to` filter and the dates must match; one-off touches one key; `learned_at` set.

## Phases

| # | Phase | Hours | Done when |
|---|---|---|---|
| 0 | Setup: venv, Windows Makefile path, key in shell only, private repo, prompt log started | 0.5 | `make grade` prints the stub's 0 |
| 1 | Foundations: docreader, llm (cache, retries, schema tool-use), facts (state IO, normalizers) | 1.5 | Re-ingest is idempotent; second run costs 0 API calls |
| 2 | Extraction: classifier + extractors (contract, invoice PDF/XLSX, amendment, notice, tracking, AWB/DGD, AP register, email assertions), grounding, scan path | 2.5 | The 8 examples extract correctly; scan reads |
| 3 | Slice: overcharge end to end (authority, pickup date, repricing, fuel, INR), reporter + evidence + schema, conflict, cannot_determine | 3 | `make eval` passes both samples; validate.py passes overcharge + conflict examples |
| 4 | Duplicate, waived, late fee (+risk), expiry, cashback (last) | 3 | Every example's expected_finding matches, decoys included |
| 5 | Policy (modes, $5k, 5 checks, 60% guard), ledger (supersedes/retro/stale), as_of filtering, confidence | 1.5 | No execute in supervised; autonomous holds back on unsafe cases |
| 6 | Feedback: 7 decisions, 5 fact types, scope matching, learned_at | 1 | Practice feedback case passes; siblings unaffected |
| 7 | `make grade`, fix causes not keys; `make sheet`, hand-check exam items | 2 | High practice card; exam sheet verified by hand |
| 8 | 10+ own evals with synthetic mailboxes; break something on purpose | 2 | evals/results.json; a case that caught a real bug |
| 9 | DESIGN, SOLUTION, AI_USE; `make validate`; commit hash | 1.5 | validate green |

If time runs short, cut cashback margin step-down and the Phase 8 extras; list them under "What I'd do next".

## Risks

- Practice overfitting: practice card is a smoke test; evals and exam sheet check generalisation.
- Hard caps at 40: obeying an email, one-off waiver made permanent, execute in supervised, >60% escalated, 3+ unsafe autonomous executes.
- Decoys are ~25% of data and false findings cost double: default to no_finding when unsure.
- Exam answer sheet is pass/fail with no key: verify every item by hand.
- Unknown per-step time limit: parallel calls and the cache.
- Cost: measure real tokens per document in Phase 2, then compute dollars.

# Solution Overview

Author: Ayush (ayushvish6555@gmail.com)

## How to run it

Requires Python 3.11+. The test and evaluation targets can be executed using the standard `make` workflow or via direct virtual environment scripts:

```bash
make setup
make grade          # Evaluates practice scenarios against answer keys; writes report_card.txt and report_card.json
make sheet          # Audits the exam mailbox into out/exam_report.json and answer_sheet.json
make eval           # Runs custom scenario evaluation cases
make validate       # Validates submission conformance (tools/check_submission.py)
```

The deterministic extraction pipeline handles the standard practice and exam mailboxes completely offline without requiring third-party API keys or incurring token costs. For scanned image files, local OCR (`rapidocr`) performs on-device transcription. When unusual layouts or unhandled conditions require external LLM fallback, `llm.py` interfaces with Anthropic using `ANTHROPIC_API_KEY`, supporting optional fallback stand-ins during development. Setting `HARNESS_NO_LLM=1` enforces strict offline mode.

Verification suites include:
- `python harness/test_foundations.py`
- `python harness/test_extraction.py`
- `python evals/run_evals.py`

## What works

- **Practice Scenario Score: 99.9 / 100** (`report_card.txt`), passing all 5 hard gates across supervised and autonomous modes with 0 external API calls. Attains 1.00 Detection F1 (22 hits, 0 misses, 0 false discoveries), 100% recovered financial recoveries ($14,140.09 of $14,140.09), flawless feedback adaptation (3/3 checks), conflict arbitration (2/2), action gating (24/24), clean control handling (16/16), and an exact 9/9 answer sheet match.
- **Exam Mailbox Audit** ([`answer_sheet.json`](file:///home/ayush/Desktop/nova-takehome/answer_sheet.json)): Uncovers total verified savings of **$8,165.33** ($3,847.70 in billing overcharges, $4,307.34 in contractual cashback, and $10.29 in avoided late fees), correctly isolating 1 rate conflict and 1 unresolvable instrument (`cannot_determine`). Hand-verified against source materials; all 59 citation pointers resolve precisely.
- **Worked Examples Verification:** Replicates expected outcomes across all eight reference fixtures, including clean controls and decoys.
- **Evaluation Suite:** Passes 48 custom evaluation scenarios spanning backdated amendments, multi-period conflict governance, standing authority scopes, and prompt injection isolation. Mutation testing (`evals/mutations.py`) deliberately modifies core rules to verify that the test suite detects semantic regressions.
- **Audit Mechanics:** Maintains temporal audit ledgers to identify superseded findings, flags backdated items with `retro`, preserves feedback adjustments via `learned_at`, and enforces the $5,000 threshold and open-conflict restrictions in autonomous gating.

## What doesn't

- **Step-down margin rebate computation:** Not modeled as no representative instances exist in the training or exam mailboxes.
- **Confidence metric calibration:** Confidence deductions rely on domain heuristics rather than an empirically fitted regression model due to the absence of false positives in the training set.
- **Generalization across non-standard free-form email prose:** While regex phrase families cover standard business correspondence, highly unconventional or idiomatic phrasing relies on the LLM fallback path.
- **Cold-start OCR startup time:** Local CPU OCR initialization incurs a short one-time model load delay on scanned PDF pages.

## The eval case that caught a bug

The scenario `feedback_one_off_month_approved` revealed an important edge case in feedback scoping. When a customer representative authorized a single month's fee waiver for a specific statement, an early iteration misapplied two separate monthly waiver notifications to the same statement line, resulting in double-crediting ($600.00 instead of $300.00). The resolution enforces line-level single-waiver consumption and validates that one-off approvals apply strictly to the designated calendar month rather than bleeding into subsequent billing cycles. The mutation test in `evals/mutations.py` verifies this constraint by artificially relaxing month boundaries and confirming immediate test failure.

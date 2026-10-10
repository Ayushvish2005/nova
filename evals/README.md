# Your eval set

Put your own golden cases in `cases/`, one YAML file each. We ship two samples, built from the worked examples, so
you can see the shape. Add at least ten of your own that go beyond ours: other layouts, other wordings, edge dates,
other ways the sources can disagree, feedback that should change an answer and feedback that shouldn't.

Each case names a mailbox folder, the date to report on, the mode, and what the report must say about one finding
key:

```yaml
name: short description
mailbox: data/examples/overcharge/docs      # a folder holding batch_01/, batch_02/ ...
as_of: "YYYY-MM-DD"
mode: supervised                            # or autonomous
feedback: {batch_01: evals/feedback/my_case.json}   # optional: fed in after that batch
expect:
  key: {invoice_id: "...", class: overcharge}
  verdict: finding                           # finding | no_finding | conflict | cannot_determine
  amount_usd: "0.00"                         # or amount_range for a conflict
  action: draft:dispute
  missing_doc: "Annex A rev 2"               # optional, for cannot_determine
why: one line on what decides it
```

Build your own mailboxes under `evals/` (copy an example's `docs/` and edit it, or write new `.eml` files). Then run:

```
make eval                 # every case; writes evals/results.json
make eval K=conflict      # only cases whose file name contains "conflict"
```

`evals/run_evals.py` starts each case from an empty state folder, ingests every batch in order, runs one report and
compares the item for your key using the grader's own matching (amounts within $0.01). It prints PASS or FAIL per
case with what differs. Put the score in your submission, and show that the set catches a regression: break
something on purpose and watch a case fail.

The runner prints how many cases are yours: every file in `cases/` whose name doesn't start with `sample_` counts,
and `make validate` wants at least ten. Cases that only point at `data/scenarios` or `data/examples` re-test what we
gave you, so build some mailboxes of your own.

## How this repo's own set was built

`evals/build/gen.py` and `evals/build/build_cases.py` generate everything below from scratch (`python evals/build/build_cases.py`):
50 cases (2 are the provided samples) over 13 synthetic mailboxes in `evals/mailboxes/` for a carrier and customer the practice and
exam data never show (Zephyrine Freight, Ardent Biologics), plus `evals/feedback/fb_decisions.json`. Each case's `why:` line gives the
arithmetic, worked out by hand from the contract tables in `gen.py` rather than from the harness.

| Group | What it checks |
|---|---|
| `pricing_*` (9) | wrong rate, rate-period switch, minimum charge, band edge, house-waybill delivery, storage days, peak season, dry-ice vs DG, in three invoice layouts |
| `waiver_*`, `injection_*` (8) | authorised, unauthorised, forwarded-only, one-off not permanent, month waiver, emailed twice, orders aimed at the AI |
| `feedback_*` (7) | one-off approval, standing authority and that it does not spread, reject, override, and the same cases without the feedback |
| `backdated_*` (2) | a backdated amendment: a new key is `retro`, a moved key `supersedes` its old id (uses `reports:` to report after batch_01) |
| `autonomy_*` (7) | 5,000 threshold, clean case executes, conflict escalates, open conflict on the same lane and period holds a sibling, a conflict in another period does not |
| `latefee_*`, `duplicate_alias_*`, `cashback_*`, `expiry_*`, `inr_*` (11) | compounding and month cut-off, Annex E alias duplicate, partial/full credit, below tier, annual credit, extension, INR conversion, missing Annex F month |
| `model_*` (4) | free-form invoice, reworded contract, conditional waiver, orders aimed at the AI: the rules decline these, so they need a model key |

`python evals/mutations.py` proves the set catches regressions: it breaks one rule at a time in a copy of the harness and checks the
guarding case fails (8 of 8 caught; a control run keeps a crashed run from counting as a catch).
Extra case keys beyond the shipped runner: `reports: {batch_01: "date"}` (take a report after that batch, as the grader does) and
`retro`, `supersedes`, `learned_at` in `expect`.

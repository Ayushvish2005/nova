"""Your harness. The grader calls exactly these three commands, in a fresh process each time:

    python harness.py ingest <batch_dir> --state-dir <dir>
    python harness.py report --as-of YYYY-MM-DD [--mode supervised|autonomous] [--state-dir <dir>] > report.json
    python harness.py feedback <feedback.json> --state-dir <dir>

- ingest: read every .eml in <batch_dir> (attachments inside) and store what you learn under --state-dir. The process
  is restarted between batches, so everything you need later must be on disk.
- report: print one report.json (grader/schema/report.schema.json) to stdout. Only documents received on or before
  --as-of count. --mode defaults to supervised.
- feedback: take in a person's decisions on what you escalated (feedback.json) and let them change later reports.

The environment gives ANTHROPIC_API_KEY and ANTHROPIC_BASE_URL. Keep the argument names as they are: grader/validate.py
checks them. Diagnostics go to stderr; report stdout contains JSON only.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# The grader executes this file from an empty working directory. Resolve package imports from the file itself.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.ingestion import ingest_batch
from harness.providers import extraction_provider
from harness.structured_ingestion import extract_pending
from harness.feedback import ingest_feedback
from harness.reporting import build_report

DEFAULT_STATE = ".state"


def extract(state_dir: str, *, batch: str | None = None, model: str | None = None, workers: int = 4) -> int:
    extractor = extraction_provider(model=model)
    try:
        summary = extract_pending(state_dir, batch=batch, extractor=extractor, workers=workers)
    finally:
        extractor.close()
    print(f"extract: {summary.extracted} extracted, {summary.reused} cached, "
          f"{summary.failed} failed documents, {summary.issues} extraction issues", file=sys.stderr)
    for problem in summary.problems:
        print(f"extract: {problem}", file=sys.stderr)
    return 1 if summary.failed else 0


def ingest(batch_dir: str, state_dir: str, *, documents_only: bool = False, model: str | None = None, workers: int = 4) -> int:
    summary = ingest_batch(batch_dir, state_dir)
    print(
        f"ingest: {summary.documents_added} new, {summary.documents_existing} existing documents; "
        f"{summary.extractions_run} extractions, {summary.extractions_reused} cached", file=sys.stderr,
    )
    for problem in summary.problems:
        print(f"ingest: {problem}", file=sys.stderr)
    extraction_status = 0 if documents_only else extract(state_dir, batch=Path(batch_dir).resolve().name, model=model, workers=workers)
    return 1 if summary.problems or extraction_status else 0


def report(as_of: str, mode: str, state_dir: str) -> dict:
    return build_report(state_dir, as_of, mode)


def feedback(feedback_file: str, state_dir: str) -> None:
    added = ingest_feedback(feedback_file, state_dir)
    print(f"feedback: {added} new items stored", file=sys.stderr)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="harness.py", description="Freight invoice audit harness")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest", help="read one batch folder into the state dir")
    p.add_argument("batch_dir")
    p.add_argument("--state-dir", default=DEFAULT_STATE)
    p.add_argument("--documents-only", action="store_true", help="ingest/OCR without calling Claude")
    p.add_argument("--model", help="model ID for the selected backend")
    p.add_argument("--workers", type=int, default=4, help="maximum concurrent document extractions")
    p = sub.add_parser("extract", help="extract structured facts from already-ingested documents")
    p.add_argument("--state-dir", default=DEFAULT_STATE)
    p.add_argument("--batch", help="restrict extraction to one ingested batch name")
    p.add_argument("--model", help="model ID for the selected backend")
    p.add_argument("--workers", type=int, default=4)
    p = sub.add_parser("report", help="print report.json for a date")
    p.add_argument("--as-of", required=True, help="YYYY-MM-DD; only documents received by then count")
    p.add_argument("--mode", choices=["supervised", "autonomous"], default="supervised")
    p.add_argument("--state-dir", default=DEFAULT_STATE)
    p = sub.add_parser("feedback", help="take in a person's decisions (feedback.json)")
    p.add_argument("feedback_file")
    p.add_argument("--state-dir", default=DEFAULT_STATE)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "ingest":
            return ingest(a.batch_dir, a.state_dir, documents_only=a.documents_only, model=a.model, workers=a.workers)
        elif a.cmd == "extract":
            return extract(a.state_dir, batch=a.batch, model=a.model, workers=a.workers)
        elif a.cmd == "report":
            import json
            json.dump(report(a.as_of, a.mode, a.state_dir), sys.stdout, indent=1)
            sys.stdout.write("\n")
        else:
            feedback(a.feedback_file, a.state_dir)
    except NotImplementedError as e:
        print(f"harness.py {a.cmd}: {e}", file=sys.stderr)
        return 3
    except (ValueError, OSError, sqlite3.Error) as e:
        print(f"harness.py {a.cmd}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
checks them. Replace the NotImplementedError bodies with your code.
"""
from __future__ import annotations

import argparse
import sys

DEFAULT_STATE = ".state"


def ingest(batch_dir: str, state_dir: str) -> None:
    import os
    import docreader
    import extraction
    import llm
    from facts import State
    st = State(state_dir)
    model = llm.LLM(os.environ.get("HARNESS_CACHE_DIR") or st.root / "llm_cache")
    todo = []
    for d in docreader.load_batch(batch_dir):
        if st.has("docs", d["doc_id"]) and st.get("docs", d["doc_id"])["sha"] == d["sha"] \
                and st.has("facts", d["doc_id"]) and st.get("facts", d["doc_id"]).get("version") == extraction.VERSION:
            continue                                 # same content already stored and read: re-ingest is a no-op
        if d["is_scan"]:
            d["pages"] = docreader.ocr_pages(d["bytes"])      # no text layer: local OCR (Claude vision could be added)
            d["ocr"] = True
        d.pop("bytes", None)
        st.put("docs", d["doc_id"], d)
        todo.append(d)

    def read(d):
        try:
            st.put("facts", d["doc_id"], extraction.extract_doc(d, model))
        except Exception as e:                       # one bad document must not lose the batch
            print(f"  extraction failed for {d['doc_id']}: {e}", file=sys.stderr)
            return 1
        return 0
    failed = sum(llm.pmap(read, todo))
    print(f"ingest {batch_dir}: {len(todo)} new document(s), {failed} failed; model calls {model.calls}, "
          f"cache hits {model.hits}", file=sys.stderr)


def report(as_of: str, mode: str, state_dir: str) -> dict:
    from facts import State
    from reporter import build_report
    return build_report(State(state_dir), as_of, mode)


def feedback(feedback_file: str, state_dir: str) -> None:
    import feedback as fb
    from facts import State
    n = fb.ingest(feedback_file, State(state_dir))
    print(f"feedback {feedback_file}: {n} new decision(s) stored", file=sys.stderr)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="harness.py", description="Freight invoice audit harness")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest", help="read one batch folder into the state dir")
    p.add_argument("batch_dir")
    p.add_argument("--state-dir", default=DEFAULT_STATE)
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
            ingest(a.batch_dir, a.state_dir)
        elif a.cmd == "report":
            import json
            json.dump(report(a.as_of, a.mode, a.state_dir), sys.stdout, indent=1)
            sys.stdout.write("\n")
        else:
            feedback(a.feedback_file, a.state_dir)
    except NotImplementedError as e:
        print(f"harness.py {a.cmd}: {e}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())

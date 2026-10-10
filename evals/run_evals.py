"""Run your eval cases (evals/cases/*.yaml) through your harness and score them. Behind `make eval`.

    python evals/run_evals.py [--harness harness/harness.py] [--cases evals/cases] [--out evals/results.json] [-k name]

Each case gets a fresh state folder: every batch_* of its mailbox is ingested in order (with a feedback file after
the batch it names, if the case has one), then one report on the case's date and mode. The item with the case's key
is compared with `expect`, using the grader's own matching (grader/score.py): verdict, amount within USD 0.01, the
range for a conflict, the action, and the missing document's name for cannot_determine. `verdict: no_finding` passes
when the key is missing or listed as no_finding. Exit code 0 when every case passes.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "grader"))
sys.path.insert(0, str(ROOT / "tools"))
import score as S  # noqa: E402
from run_mailbox import run  # noqa: E402


def find_item(report: dict, key: dict) -> dict | None:
    want = S.key_tuple(key)
    for it in S.report_items(report):
        if S.key_tuple(it) == want:
            return it
    return None


def check(case: dict, item: dict | None) -> list[str]:
    """-> what differs (empty when the case passes)."""
    exp = case["expect"]
    v = exp.get("verdict", "finding")
    if v == "no_finding":
        return [] if item is None or item.get("verdict") == "no_finding" else \
            [f"expected no finding, got {item.get('verdict')} {item.get('amount_usd') or ''}".strip()]
    if item is None:
        return ["key missing from the report"]
    bad = []
    if item.get("verdict") != v:
        bad.append(f"verdict {item.get('verdict')}, expected {v}")
    if "amount_usd" in exp and exp["amount_usd"] is not None and not S.amount_ok(item.get("amount_usd"), exp["amount_usd"]):
        bad.append(f"amount {item.get('amount_usd')}, expected {exp['amount_usd']}")
    if exp.get("amount_range") and not S.range_ok(item.get("amount_range"), exp["amount_range"]):
        bad.append(f"range {item.get('amount_range')}, expected {exp['amount_range']}")
    if exp.get("action") and str(item.get("action")) != exp["action"]:
        bad.append(f"action {item.get('action')}, expected {exp['action']}")
    if exp.get("deadline") and str(item.get("deadline")) != str(exp["deadline"]):
        bad.append(f"deadline {item.get('deadline')}, expected {exp['deadline']}")
    if "retro" in exp and bool(item.get("retro")) != bool(exp["retro"]):
        bad.append(f"retro {bool(item.get('retro'))}, expected {bool(exp['retro'])}")
    if "supersedes" in exp and list(item.get("supersedes") or []) != list(exp["supersedes"]):
        bad.append(f"supersedes {item.get('supersedes')}, expected {exp['supersedes']}")
    if "learned_at" in exp and bool(item.get("learned_at")) != bool(exp["learned_at"]):
        bad.append(f"learned_at {item.get('learned_at')}, expected {bool(exp['learned_at'])}")
    want_doc = (exp.get("missing_doc") or {}).get("name") if isinstance(exp.get("missing_doc"), dict) else exp.get("missing_doc")
    if want_doc:
        got = ((item.get("missing_doc") or {}).get("name") or "").lower()
        if not all(w in got for w in str(want_doc).lower().split()):
            bad.append(f"missing_doc {got or 'none'}, expected {want_doc}")
    return bad


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="run_evals.py", description="score your eval cases")
    ap.add_argument("--harness", default="harness/harness.py")
    ap.add_argument("--cases", default=str(ROOT / "evals" / "cases"))
    ap.add_argument("--out", default=str(ROOT / "evals" / "results.json"))
    ap.add_argument("-k", help="only cases whose file name contains this")
    ap.add_argument("--timeout", type=float, default=600.0)
    a = ap.parse_args(argv)
    files = sorted(Path(a.cases).glob("*.yaml"))
    if a.k:
        files = [f for f in files if a.k in f.name]
    if not files:
        print(f"no cases in {a.cases}", file=sys.stderr)
        return 2
    results = []
    for f in files:
        case = yaml.safe_load(f.read_text())
        mailbox = (ROOT / case["mailbox"]) if not Path(case["mailbox"]).is_absolute() else Path(case["mailbox"])
        fb = {k: (ROOT / v) for k, v in (case.get("feedback") or {}).items()}   # {batch_02: path/to/feedback.json}
        with tempfile.TemporaryDirectory(prefix="eval-") as tmp:
            report, errors = run(a.harness, mailbox, str(case["as_of"]), case.get("mode", "supervised"),
                                 Path(tmp) / "state", a.timeout, fb, log=lambda *_: None, mid=case.get("reports"))
        diff = errors if report is None else check(case, find_item(report, case["expect"]["key"]))
        ok = not diff
        results.append({"case": f.name, "name": case.get("name"), "pass": ok, "diff": diff})
        print(f"{'PASS' if ok else 'FAIL'}  {f.name}: {case.get('name', '')}" + ("" if ok else f"\n      {'; '.join(diff)[:400]}"))
    n = sum(r["pass"] for r in results)
    ours = sum(1 for r in results if r["case"].startswith("sample_"))
    summary = {"harness": a.harness, "passed": n, "total": len(results), "your_cases": len(results) - ours,
               "score": round(n / len(results), 4), "cases": results}
    if not a.k:                                  # a filtered run never overwrites the full results
        Path(a.out).write_text(json.dumps(summary, indent=1) + "\n")
    print(f"\n{n} of {len(results)} cases pass ({len(results) - ours} of them yours)" + ("" if a.k else f"; wrote {a.out}"))
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

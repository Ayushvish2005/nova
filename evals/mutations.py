"""Show that the eval set catches regressions: break one rule in a COPY of the harness, run the case that guards it, expect FAIL.

    python evals/mutations.py            # runs every mutation below, prints whether the guarding case failed (good) or passed (gap)

The real harness is never touched. Each mutation re-creates a bug that was found while building the set (or a rule the brief
calls out), so CAUGHT means the case would have caught it. A control run (the unmutated copy must pass the case) guards against
a run that merely crashed being counted as a catch. Several rules have two independent guards, so those mutations break both.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (name, file, [(regex, replacement), ...] applied in order, guarding case file-name fragment)
MUTATIONS = [
    ("event lookup ignores the house waybill id", "harness/world.py",
     [(r'for k in \{awb, awb\.split\("-H"\)\[0\]\}', 'for k in {awb.split("-H")[0]}')], "pricing_house_waybill_collected"),
    ("a waived line counted once per email (main path)", "harness/billing_checks.py",
     [(r"break\s+# a line is waived once, however many emails say so", "pass")], "waiver_emailed_twice"),
    ("a waived line counted once per email (one-off path)", "harness/billing_checks.py",
     [(r"(learned\.append\(one_off\)\n\s+evs \+= [^\n]+\n)\s+break", r"\1"),
      (r'if a\.get\("months"\) and when not in a\["months"\]:', "if False:")], "feedback_one_off_month_approved"),
    ("a waiver covers any shipment (one-off treated as permanent)", "harness/billing_checks.py",
     [(r"named = awb in \[x\.split\(\"-H\"\)\[0\] for x in a\.get\(\"awbs\"\) or \[\]\] if awb and a\.get\(\"awbs\"\) else \\\n\s+bool\(a\.get\(\"months\"\)\) and _when\(l, b\) in a\[\"months\"\]",
       "named = True")], "waiver_not_permanent"),
    ("quoted waivers are not recorded", "harness/rule_parser.py", [(r"out \+= _quoted_waivers\(lines, q\)", "pass")], "waiver_forwarded_only"),
    ("effective date is not read", "harness/rule_parser.py", [(r'ed = re\.search\(r"Effective \[Dd\]ate', 'ed = re.search(r"Zzz nothing')],
     "cashback_annual_credit"),
    ("open conflicts ignore the rate period", "harness/reporter.py",
     [(r"if any\(\(raw\.get\(\"cid\"\), lane\) in open_conflicts for lane in raw\.get\(\"lanes\"\) or \[\]\)",
       'if any((raw.get("cid"), lane[0]) in {(c, l[0]) for c, l in open_conflicts} for lane in raw.get("lanes") or [])')],
     "autonomy_conflict_in_other_period_executes"),
    ("a confirmed authority is not tied to the person it names", "harness/feedback.py",
     [(r'if k in ap and \(_name\(ap\[k\]\) != _name\(v\) if k == "person" else str\(ap\[k\]\)\.upper\(\) != str\(v\)\.upper\(\)\):\n\s+return False', "pass"),
      (r'_name\(sub\.get\("person"\)\) == _name\(person\) and ', "")], "feedback_standing_authority_not_spread"),
]


def run_case(harness_py: Path, frag: str) -> tuple[bool, str]:
    """-> (passed, the runner's explanation). A run that cannot even start is not a pass and not a catch: it raises."""
    env = dict(os.environ, PYTHONUTF8="1", HARNESS_NO_LLM="1")
    r = subprocess.run([sys.executable, str(ROOT / "evals" / "run_evals.py"), "--harness", harness_py.as_posix(), "-k", frag],
                       capture_output=True, text=True, env=env, cwd=ROOT)      # as_posix: the runner shlex-splits the path
    if not re.search(r"(PASS|FAIL)\s+" + re.escape(frag), r.stdout):
        raise RuntimeError(f"case {frag} did not run: {r.stdout[-300:]} {r.stderr[-300:]}")
    fail = re.search(r"FAIL\s+" + re.escape(frag) + r"[^\n]*\n\s+([^\n]+)", r.stdout)
    return "FAIL" not in r.stdout, (fail[1][:110] if fail else "")


def main() -> int:
    gaps = 0
    ignore = shutil.ignore_patterns("__pycache__", "test_*.py")
    for name, rel, steps, case in MUTATIONS:
        with tempfile.TemporaryDirectory() as t:
            copy, control = Path(t) / "harness", Path(t) / "control"
            shutil.copytree(ROOT / "harness", copy, ignore=ignore)
            shutil.copytree(ROOT / "harness", control, ignore=ignore)
            f = copy / Path(rel).name
            src = f.read_text(encoding="utf-8")
            for pattern, repl in steps:
                src, n = re.subn(pattern, repl, src, count=1)
                if not n:
                    raise RuntimeError(f"mutation '{name}': pattern not found in {rel}: {pattern[:60]}")
            f.write_text(src, encoding="utf-8")
            ok, _ = run_case(control / "harness.py", case)
            if not ok:
                raise RuntimeError(f"control failed: {case} does not pass on the unmutated harness")
            passed, why = run_case(copy / "harness.py", case)
            gaps += passed
            print(f"{'MISSED' if passed else 'CAUGHT'}  {name}  ->  {case}" + (f"\n        {why}" if why else ""))
    print(f"\n{len(MUTATIONS) - gaps} of {len(MUTATIONS)} mutations caught")
    return 1 if gaps else 0


if __name__ == "__main__":
    sys.exit(main())

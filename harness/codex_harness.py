"""Local Codex wrapper: make grade/sheet/eval HARNESS=harness/codex_harness.py.

Uses the Python SDK and its pinned runtime with an existing Codex sign-in.
"""
from __future__ import annotations

import os
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.harness import main

if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--codex-home", help="existing Codex authentication directory for the grader's fresh HOME")
    parser.add_argument("--codex-cache-dir", help="optional local development response cache, outside fresh grader state")
    parser.add_argument("--codex-reasoning-effort", choices=["low", "medium", "high", "xhigh", "max", "ultra"],
                        help="explicit SDK reasoning setting (model support varies)")
    options, arguments = parser.parse_known_args()
    if options.codex_home:
        os.environ["CODEX_HOME"] = str(Path(options.codex_home).resolve())
    if options.codex_cache_dir:
        os.environ["CODEX_CACHE_DIR"] = str(Path(options.codex_cache_dir).resolve())
    if options.codex_reasoning_effort:
        os.environ["CODEX_REASONING_EFFORT"] = options.codex_reasoning_effort
    if arguments and arguments[0] in ("ingest", "extract") and "--workers" not in arguments:
        # Bound concurrent document requests for the local SDK transport.
        arguments.extend(["--workers", "8"])
    os.environ["HARNESS_BACKEND"] = "codex"
    sys.exit(main(arguments))

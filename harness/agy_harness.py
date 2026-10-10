"""Local AGY wrapper; the grader should use harness.py with its Anthropic key."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.harness import main


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--agy-model", help="optional AGY model ID")
    parser.add_argument("--agy-effort", choices=["low", "medium", "high", "xhigh", "max"],
                        help="AGY reasoning effort; defaults to low")
    parser.add_argument("--agy-cache-dir", help="private local response cache")
    parser.add_argument("--agy-timeout", type=float, help="per-call timeout in seconds")
    options, arguments = parser.parse_known_args()
    if options.agy_model:
        os.environ["AGY_MODEL"] = options.agy_model
    if options.agy_effort:
        os.environ["AGY_EFFORT"] = options.agy_effort
    if options.agy_cache_dir:
        os.environ["AGY_CACHE_DIR"] = str(Path(options.agy_cache_dir).resolve())
    if options.agy_timeout is not None:
        os.environ["AGY_TIMEOUT"] = str(options.agy_timeout)
    os.environ["HARNESS_BACKEND"] = "agy"
    sys.exit(main(arguments))

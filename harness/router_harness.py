"""Explicit Groq/OpenRouter/Gemini wrapper; grading still uses harness.py."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.harness import main


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--groq-model")
    parser.add_argument("--openrouter-model")
    parser.add_argument("--gemini-model")
    parser.add_argument("--router-cache-dir")
    parser.add_argument("--router-timeout", type=float)
    options, arguments = parser.parse_known_args()
    for name, value in (
        ("GROQ_MODEL", options.groq_model),
        ("OPENROUTER_MODEL", options.openrouter_model),
        ("GEMINI_MODEL", options.gemini_model),
        ("ROUTER_TIMEOUT", options.router_timeout),
    ):
        if value is not None:
            os.environ[name] = str(value)
    if options.router_cache_dir:
        os.environ["ROUTER_CACHE_DIR"] = str(Path(options.router_cache_dir).resolve())
    os.environ["HARNESS_BACKEND"] = "router"
    sys.exit(main(arguments))

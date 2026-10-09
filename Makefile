# Nova take-home. `make help` lists the targets.
# Every target runs in .venv (made by `make setup`). HARNESS points at your harness; MODE is supervised, autonomous
# or both (the default for make grade).
PY      ?= .venv/bin/python
HARNESS ?= harness/harness.py
MODE    ?= both

.PHONY: help setup venv run grade sheet eval test validate clean
help:
	@echo "make setup      create .venv and install harness/requirements.txt"
	@echo "make grade      replay the practice mailbox through your harness; writes report_card.txt and .json"
	@echo "make sheet      run the exam mailbox; writes out/exam_report.json and answer_sheet.json"
	@echo "make eval       run your cases in evals/cases (K=<text> runs only matching files)"
	@echo "make test       run offline ingestion, extraction, feedback and report tests"
	@echo "make validate   check the repo is ready to send (SUBMISSION.md)"
	@echo "make run        run the practice mailbox once; writes out/practice_report.json"

setup:
	@if command -v uv >/dev/null 2>&1; then \
		uv venv -q --allow-existing --python ">=3.11" .venv && uv pip install -q --python .venv/bin/python -r harness/requirements.txt; \
	else \
		python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || { echo "need Python 3.11 or newer"; exit 2; }; \
		python3 -m venv .venv && .venv/bin/pip install -q -r harness/requirements.txt; \
	fi
	@$(PY) -c "import pymupdf, openpyxl, jsonschema, yaml" && echo "setup done. Next: write harness/harness.py, then make grade"

venv:
	@test -x "$(PY)" || command -v "$(PY)" >/dev/null 2>&1 || { echo "no $(PY) yet: run make setup first"; exit 2; }

run: venv
	$(PY) tools/run_mailbox.py --data data/scenarios --harness "$(HARNESS)" --out out/practice_report.json

grade: venv
	@$(PY) grader/run_grader.py --harness "python $(HARNESS)" --data data/scenarios --key data/scenarios/answers \
		--mode $(MODE) --out report_card.json > report_card.txt; rc=$$?; cat report_card.txt; exit $$rc

sheet: venv
	$(PY) tools/run_mailbox.py --data data/exam --harness "$(HARNESS)" --out out/exam_report.json --sheet answer_sheet.json

eval: venv
	$(PY) evals/run_evals.py --harness "$(HARNESS)" $(if $(K),-k $(K),)

test: venv
	$(PY) -m unittest discover -s tests -v

validate: venv
	@$(PY) tools/check_submission.py --harness $(HARNESS)

clean:
	rm -rf out/state_* .state

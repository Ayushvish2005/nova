"""Phase 2 self-check: python harness/test_extraction.py
Reads documents through the rule-based readers (the model is switched off) and asserts the facts the worked examples'
arithmetic depends on. Free and deterministic; the scan test runs local OCR (about a minute).
"""
import os
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "harness"))
os.environ["HARNESS_NO_LLM"] = "1"
os.environ.setdefault("HARNESS_CACHE_DIR", str(ROOT / ".cache" / "llm"))
import docreader  # noqa: E402
import extraction as X  # noqa: E402
import llm  # noqa: E402

M = llm.LLM(os.environ["HARNESS_CACHE_DIR"])


def facts_of(mailbox: str, name: str) -> dict:
    for b in sorted((ROOT / mailbox).glob("batch_*")):
        for d in docreader.load_batch(b):
            if d["doc_id"].endswith(name):
                if d["is_scan"]:
                    d["pages"] = docreader.ocr_pages(d["bytes"])
                d.pop("bytes", None)
                return X.extract_doc(d, M)
    raise KeyError(name)


def test_contract_tables_and_terms():
    f = facts_of("data/examples/overcharge/docs", "MSA-TBX-US-EXP-E60.pdf")
    assert f["kind"] == "contract" and f["warnings"] == [] and f["via"] == "rules", f["warnings"]
    d = f["data"]
    row = next(r for r in d["rate_rows"] if r["lane"] == "USE2-INE2" and r["band_min"] == "100")
    assert (row["v1"], row["v2"]) == ("6.01", "6.16") and d["rate_columns"] == ["P1", "P2"]
    assert next(r for r in d["fuel_rows"] if r["month"] == "2031-12")["pct"].rstrip("%") == "20.16"
    assert d["terms"]["payment_days"] in (45, "45") and Decimal(str(d["terms"]["late_fee_pct_per_month"]).rstrip("%")) == Decimal("1.19")
    assert any(p["categories"] == ["rates"] for p in d["people"]) and len(d["code_map"]) == 18


def test_invoice_lines_and_grounding():
    f = facts_of("data/examples/overcharge/docs", "TX-US-2032-5394.pdf")
    d = f["data"]
    assert f["kind"] == "billing" and d["number"] == "TX-US-2032-5394" and f["warnings"] == []
    line = d["lines"][0]
    assert (line["awb"], line["qty"], line["rate"], line["amount"]) == ("TE56915687", "241", "6.69", "1612.29")
    assert all(not l["flags"] for l in d["lines"])


def test_tracking_and_ap_register_are_parsed_by_code():
    t = facts_of("data/examples/overcharge/docs", "tracking-2031-07-01.xlsx")
    assert t["kind"] == "tracking"
    assert {"awb": "TE56915687", "event": "PICKED_UP", "date": "2031-12-25"}.items() <= next(
        r for r in t["data"]["rows"] if r["event"] == "PICKED_UP").items()
    a = facts_of("data/scenarios", "ap-register-2035-05-31.xlsx")
    assert a["kind"] == "ap_register" and len(a["data"]["rows"]) == 26
    r = a["data"]["rows"][0]
    assert (r["invoice_no"], r["due_date"], r["status"], r["paid_date"], r["amount"]) == (
        "TX-US-2034-0379", "2035-01-10", "paid", "2035-01-03", "1243.96")


def test_waiver_email_and_quoted_text():
    f = facts_of("data/scenarios", "batch_01/0004.eml")
    w = [a for a in f["data"]["assertions"] if a["type"] == "waiver"]
    assert len(w) == 1 and w[0]["awbs"] == ["TX45998993"] and not w[0]["in_quoted_text"]
    # a conditional waiver is not for the rules to judge: it must be handed to the model
    body = "From: a <a@b>\nTo: c\nDate: d\nSubject: s\n\nWe waive the Delivery fee on AWB TX12345678 if the carrier agrees."
    cond = {"doc_id": "x", "kind": "email", "pages": [body], "subject": "s"}
    assert X.rule_parser.parse(cond, "email") is None
    # the same sentence quoted back by someone else must be flagged as quoted
    page = "From: x\nTo: y\nDate: d\nSubject: s\n\nFYI below\n> we waive the Delivery fee on AWB TX1.\n> thanks"
    assert X.quoted_lines(page) == {7, 8}


def test_amendment_cites_missing_annex_and_scan_reads():
    f = facts_of("data/scenarios", "TBX-US-EXP-01-AMD-1.pdf")
    d = f["data"]
    assert f["kind"] == "amendment" and d["effective_from"] == "2034-11-11"
    assert any(c["name"] == "Annex A rev 2" and c["lanes"] == ["USS2-INN1"] for c in d["cited_documents"])
    assert any(r["lane"] == "USC3-USS2" and r["v1"] == "3.12" for r in d["rate_rows"])
    s = facts_of("data/exam", "CDI-3031-0306.pdf")
    assert s["kind"] == "billing" and s["data"]["currency"] == "INR" and s["data"]["total"] == "127024.44"
    assert s["data"]["lines"][0]["amount"] == "90535.08" and s["data"]["lines"][0]["date"] == "2031-01-06"


def test_grounding_helpers():
    assert X.grounded("1612.29", "1 TE56915687 2031-12-25 Air freight 241 kg 6.69 1,612.29")
    assert X.grounded("6.690", "rate 6.69") and not X.grounded("6.70", "rate 6.69")
    assert X.parse_table(["A | B\nTX1 | PICKED_UP | 2031-12-25 | USE2 | -"], "tracking", 1,
                         ["awb", "event", "date", "station", "reason_code"])[0]["date"] == "2031-12-25"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok ", name)
    print(f"model calls {M.calls}, cache hits {M.hits}")
